import io
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class BackupToolTests(unittest.TestCase):
    def run_tool(self, tool: str, archive: Path, destination: Path):
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / tool), str(archive), str(destination)],
            capture_output=True,
            text=True,
        )

    def make_fake_age(self, root: Path) -> Path:
        executable = root / "fake-age"
        executable.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import pathlib
                import sys

                args = sys.argv[1:]
                output = pathlib.Path(args[args.index("--output") + 1])
                source = pathlib.Path(args[-1])
                if "--encrypt" in args:
                    recipient = args[args.index("--recipient") + 1]
                    output.write_bytes(b"FAKE-AGE:" + recipient.encode() + b"\\n" + source.read_bytes())
                    raise SystemExit(0)
                identity = pathlib.Path(args[args.index("--identity") + 1]).read_text().strip()
                payload = source.read_bytes()
                prefix, separator, plaintext = payload.partition(b"\\n")
                expected = b"FAKE-AGE:" + identity.encode()
                if not separator or prefix != expected:
                    raise SystemExit(42)
                output.write_bytes(plaintext)
                """
            )
        )
        executable.chmod(0o700)
        return executable

    def make_fake_docker(self, root: Path) -> tuple[Path, Path]:
        binary_dir = root / "bin"
        binary_dir.mkdir()
        log = root / "docker.log"
        executable = binary_dir / "docker"
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$FAKE_DOCKER_LOG"\n')
        executable.chmod(0o700)
        return binary_dir, log

    def run_common(self, command: str, *arguments: str, env=None, cwd=None):
        return subprocess.run(
            ["bash", "-c", f'source "$1"; {command}', "test", str(ROOT / "scripts/ops_common.sh"), *arguments],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
        )

    def test_bundle_rejects_symlink_member(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name in ("manifest.json", "SHA256SUMS", "database.dump"):
                    info = tarfile.TarInfo(name)
                    info.size = 0
                    bundle.addfile(info, io.BytesIO())
                link = tarfile.TarInfo("uploads.tar")
                link.type = tarfile.SYMTYPE
                link.linkname = "/etc/passwd"
                bundle.addfile(link)
            result = self.run_tool("unpack_backup.py", archive, root / "out")
            self.assertNotEqual(result.returncode, 0)

    def test_uploads_reject_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "uploads.tar"
            destination = root / "out"
            destination.mkdir()
            with tarfile.open(archive, "w:") as bundle:
                valid = tarfile.TarInfo("first.txt")
                valid.size = 1
                bundle.addfile(valid, io.BytesIO(b"x"))
                info = tarfile.TarInfo("../escape.txt")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
            result = self.run_tool("unpack_uploads.py", archive, destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "escape.txt").exists())
            self.assertFalse((destination / "first.txt").exists())

    def test_uploads_extract_regular_flat_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "uploads.tar"
            destination = root / "out"
            destination.mkdir()
            with tarfile.open(archive, "w:") as bundle:
                info = tarfile.TarInfo("./digest.md")
                info.size = 4
                bundle.addfile(info, io.BytesIO(b"safe"))
            result = self.run_tool("unpack_uploads.py", archive, destination)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / "digest.md").read_bytes(), b"safe")

    def test_age_round_trip_and_wrong_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_age = self.make_fake_age(root)
            source = root / "source.tar.gz"
            encrypted = root / "backup.age"
            restored = root / "restored.tar.gz"
            identity = root / "identity.txt"
            wrong_identity = root / "wrong.txt"
            source.write_bytes(b"private backup bytes")
            identity.write_text("age1-correct")
            wrong_identity.write_text("age1-wrong")
            identity.chmod(0o600)
            wrong_identity.chmod(0o600)
            environment = os.environ | {"ATLAS_AGE_BIN": str(fake_age)}

            encrypted_result = self.run_common(
                'ops_encrypt_file "$2" "$3" "$4"',
                "age1-correct",
                str(source),
                str(encrypted),
                env=environment,
            )
            self.assertEqual(encrypted_result.returncode, 0, encrypted_result.stderr)
            decrypted_result = self.run_common(
                'ops_decrypt_file "$2" "$3" "$4"',
                str(identity),
                str(encrypted),
                str(restored),
                env=environment,
            )
            self.assertEqual(decrypted_result.returncode, 0, decrypted_result.stderr)
            self.assertEqual(restored.read_bytes(), source.read_bytes())

            restored.unlink()
            wrong_result = self.run_common(
                'ops_decrypt_file "$2" "$3" "$4"',
                str(wrong_identity),
                str(encrypted),
                str(restored),
                env=environment,
            )
            self.assertNotEqual(wrong_result.returncode, 0)
            self.assertFalse(restored.exists())
            encrypted.write_bytes(b"corrupt ciphertext")
            corrupt_result = self.run_common(
                'ops_decrypt_file "$2" "$3" "$4"',
                str(identity),
                str(encrypted),
                str(restored),
                env=environment,
            )
            self.assertNotEqual(corrupt_result.returncode, 0)
            self.assertFalse(restored.exists())

    def test_backup_and_restore_share_nonblocking_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_file = Path(directory) / "ops.lock"
            environment = os.environ | {"ATLAS_OPS_LOCK_FILE": str(lock_file)}
            holder = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'source "$1"; ops_acquire_lock; echo locked; sleep 30',
                    "holder",
                    str(ROOT / "scripts/ops_common.sh"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                contender = self.run_common("ops_acquire_lock", env=environment)
                self.assertEqual(contender.returncode, 4)
            finally:
                holder.terminate()
                holder.communicate(timeout=3)

    def test_default_lock_uses_compose_project_across_checkouts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            first_checkout = root / "first" / "checkout"
            second_checkout = root / "second" / "checkout"
            runtime.mkdir(mode=0o700)
            first_checkout.mkdir(parents=True)
            second_checkout.mkdir(parents=True)
            environment = os.environ | {
                "XDG_RUNTIME_DIR": str(runtime),
                "COMPOSE_PROJECT_NAME": "shared-atlas",
            }
            holder = subprocess.Popen(
                [
                    "bash", "-c",
                    'source "$1"; ops_acquire_lock; echo locked; sleep 30',
                    "holder", str(ROOT / "scripts/ops_common.sh"),
                ],
                cwd=first_checkout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                same_project = self.run_common(
                    "ops_acquire_lock", env=environment, cwd=second_checkout
                )
                self.assertEqual(same_project.returncode, 4, same_project.stderr)
                other_project = self.run_common(
                    "ops_acquire_lock",
                    env=environment | {"COMPOSE_PROJECT_NAME": "other-atlas"},
                    cwd=second_checkout,
                )
                self.assertEqual(other_project.returncode, 0, other_project.stderr)
            finally:
                holder.terminate()
                holder.communicate(timeout=3)

    def test_lock_rejects_symlink_and_unsafe_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            victim = root / "victim"
            victim.write_text("do not alter")
            victim.chmod(0o600)
            symlink_lock = root / "linked.lock"
            symlink_lock.symlink_to(victim)
            linked = self.run_common(
                "ops_acquire_lock",
                env=os.environ | {"ATLAS_OPS_LOCK_FILE": str(symlink_lock)},
            )
            self.assertEqual(linked.returncode, 3)
            self.assertEqual(victim.read_text(), "do not alter")

            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            unsafe.chmod(0o755)
            unsafe_result = self.run_common(
                "ops_acquire_lock",
                env=os.environ | {"ATLAS_OPS_LOCK_FILE": str(unsafe / "ops.lock")},
            )
            self.assertEqual(unsafe_result.returncode, 3)
            self.assertFalse((unsafe / "ops.lock").exists())

    def test_default_lock_rejects_unsafe_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o755)
            runtime.chmod(0o755)
            result = self.run_common(
                "ops_acquire_lock",
                env=os.environ | {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "COMPOSE_PROJECT_NAME": "atlas",
                },
            )
            self.assertEqual(result.returncode, 3)

    def test_decrypt_rejects_public_or_wrong_owner_identity_before_age(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "age-called"
            fake_age = root / "never-age"
            fake_age.write_text('#!/bin/sh\ntouch "$FAKE_AGE_MARKER"\nexit 99\n')
            fake_age.chmod(0o700)
            ciphertext = root / "backup.age"
            ciphertext.write_bytes(b"ciphertext")
            output = root / "output"
            identity = root / "identity.txt"
            identity.write_text("AGE-SECRET-KEY-test")
            identity.chmod(0o644)
            environment = os.environ | {
                "ATLAS_AGE_BIN": str(fake_age),
                "FAKE_AGE_MARKER": str(marker),
            }
            public = self.run_common(
                'ops_decrypt_file "$2" "$3" "$4"',
                str(identity), str(ciphertext), str(output), env=environment,
            )
            self.assertEqual(public.returncode, 3)
            self.assertFalse(marker.exists())

            if os.geteuid() == 0:
                identity.chmod(0o600)
                os.chown(identity, 65534, 65534)
                wrong_owner = self.run_common(
                    'ops_decrypt_file "$2" "$3" "$4"',
                    str(identity), str(ciphertext), str(output), env=environment,
                )
                self.assertEqual(wrong_owner.returncode, 3)
                self.assertFalse(marker.exists())

    def test_signal_handlers_return_stable_codes_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker"
            process = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'source "$1"; ops_install_signal_handlers; trap \'rm -f -- "$2"\' EXIT; '
                    'touch "$2"; while :; do sleep 0.1; done',
                    "signals",
                    str(ROOT / "scripts/ops_common.sh"),
                    str(marker),
                ]
            )
            for _attempt in range(50):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.terminate()
            self.assertEqual(process.wait(timeout=3), 143)
            self.assertFalse(marker.exists())

    def test_restore_lists_dump_before_mutation(self):
        script = (ROOT / "scripts/restore.sh").read_text()
        list_position = script.index("pg_restore --list")
        mutation_position = script.index("mutation_started=true")
        drop_position = script.index("DROP DATABASE")
        self.assertLess(list_position, mutation_position)
        self.assertLess(list_position, drop_position)

    def test_restore_wrong_key_and_corruption_never_mutate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            fake_age = self.make_fake_age(root)
            docker_dir, docker_log = self.make_fake_docker(root)
            source = root / "bundle.tar.gz"
            encrypted = root / "backup.age"
            correct_identity = root / "correct.txt"
            wrong_identity = root / "wrong.txt"
            source.write_bytes(b"synthetic bundle")
            correct_identity.write_text("age1-correct")
            wrong_identity.write_text("age1-wrong")
            correct_identity.chmod(0o600)
            wrong_identity.chmod(0o600)
            environment = os.environ | {
                "ATLAS_AGE_BIN": str(fake_age),
                "ATLAS_OPS_LOCK_FILE": str(root / "ops.lock"),
                "FAKE_DOCKER_LOG": str(docker_log),
                "PATH": f"{docker_dir}:{os.environ['PATH']}",
                "TMPDIR": str(root),
            }
            encrypted_result = self.run_common(
                'ops_encrypt_file "$2" "$3" "$4"',
                "age1-correct",
                str(source),
                str(encrypted),
                env=environment,
            )
            self.assertEqual(encrypted_result.returncode, 0)

            wrong = subprocess.run(
                [str(ROOT / "scripts/restore.sh"), "--identity", str(wrong_identity), str(encrypted)],
                cwd=root,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(wrong.returncode, 6)
            encrypted.write_bytes(b"corrupt ciphertext")
            corrupt = subprocess.run(
                [str(ROOT / "scripts/restore.sh"), "--identity", str(correct_identity), str(encrypted)],
                cwd=root,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(corrupt.returncode, 6)
            docker_calls = docker_log.read_text().splitlines()
            self.assertTrue(all(call == "compose ps --status running --services" for call in docker_calls))

    def test_interrupted_restore_returns_143_without_mutation_or_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            docker_dir, docker_log = self.make_fake_docker(root)
            marker = root / "age-started"
            slow_age = root / "slow-age"
            slow_age.write_text(
                "#!/bin/sh\ntouch \"$FAKE_AGE_MARKER\"\ntrap 'exit 143' TERM INT\nwhile :; do sleep 1; done\n"
            )
            slow_age.chmod(0o700)
            archive = root / "backup.age"
            identity = root / "identity.txt"
            archive.write_bytes(b"ciphertext")
            identity.write_text("age1-test")
            identity.chmod(0o600)
            environment = os.environ | {
                "ATLAS_AGE_BIN": str(slow_age),
                "ATLAS_OPS_LOCK_FILE": str(root / "ops.lock"),
                "FAKE_AGE_MARKER": str(marker),
                "FAKE_DOCKER_LOG": str(docker_log),
                "PATH": f"{docker_dir}:{os.environ['PATH']}",
                "TMPDIR": str(root),
            }
            process = subprocess.Popen(
                [str(ROOT / "scripts/restore.sh"), "--identity", str(identity), str(archive)],
                cwd=root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            for _attempt in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=5)

            self.assertEqual(process.returncode, 143)
            docker_calls = docker_log.read_text().splitlines()
            self.assertEqual(docker_calls, ["compose ps --status running --services"])
            self.assertEqual(list(root.glob("atlas-restore.*")), [])
            self.assertEqual(list(root.glob(".atlas-uploads-restore.*")), [])


if __name__ == "__main__":
    unittest.main()
