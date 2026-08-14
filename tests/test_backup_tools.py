import io
import subprocess
import sys
import tarfile
import tempfile
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
                info = tarfile.TarInfo("../escape.txt")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))
            result = self.run_tool("unpack_uploads.py", archive, destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "escape.txt").exists())

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


if __name__ == "__main__":
    unittest.main()

