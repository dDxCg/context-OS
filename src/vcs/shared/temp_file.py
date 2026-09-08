import os
from pathlib import Path
import shutil
from utils.helper import anchored, save_to_file, gen_hash, make_dirs, path_normalize, read_file

class TempFile:
    TMP_DIR = Path(anchored(os.getenv("TMP_DIR", "data/tmp")))
    def __init__(self, content):
        make_dirs(self.TMP_DIR)
        self.path = Path(f"{self.TMP_DIR}/{gen_hash(content)}.blob")
        self.create_tmp_file(content)

    @classmethod
    def from_path(cls, path, read_mode="rb"):
        path = path_normalize(path)
        content = read_file(path, mode=read_mode)
        return cls(content)

    def create_tmp_file(self, content):
        if type(content) is bytes:
            save_to_file(content, self.path, mode="wb")
        else:
            save_to_file(content, self.path, mode="w")

    def move_tmp_file(self, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(self.path, target_path)

    def delete_tmp_file(self):
        self.path.unlink(missing_ok=True)
        # self.TMP_DIR.rmdir()

    def read_bytes(self):
        return self.path.read_bytes()