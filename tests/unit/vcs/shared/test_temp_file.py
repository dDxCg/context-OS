from utils.helper import PROJECT_ROOT
from vcs.shared.temp_file import TempFile


def test_ac1_tmp_dir_is_anchored_to_project_root():
    assert TempFile.TMP_DIR == PROJECT_ROOT / "data" / "tmp"
