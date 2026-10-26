"""私有图像依赖必须进入安装包，且不允许篡改或夹带文件。"""
import importlib.util
from pathlib import Path
import tempfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("image_packager",ROOT/"tools/package_ankiaddon.py")
packager=importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)

def test_vendor_manifest_matches_required_native_files():
    names={name for _,name in packager.package_files(ROOT)}
    assert "vendor/PIL/Image.py" in names
    assert "vendor/PIL/_imaging.cpython-313-darwin.so" in names
    assert "vendor/licenses/Pillow-LICENSE.txt" in names
    assert not any("__pycache__" in name for name in names)

def test_vendor_rejects_unknown_and_missing_files(tmp_path):
    import shutil
    vendor=tmp_path/"vendor"
    shutil.copytree(ROOT/"vendor",vendor,ignore=shutil.ignore_patterns("__pycache__"))
    unknown=vendor/"PIL/unexpected.py"
    unknown.write_text("raise RuntimeError()")
    with pytest.raises(packager.PackageError,match="checksum mismatch"):
        packager._collect_vendor_files(tmp_path,[])
    unknown.unlink()
    (vendor/"PIL/_imaging.cpython-313-darwin.so").unlink()
    with pytest.raises(packager.PackageError,match="files missing"):
        packager._collect_vendor_files(tmp_path,[])
