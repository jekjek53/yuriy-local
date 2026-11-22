"""Windows path regression tests runnable on POSIX; one live NTFS test."""
import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
import pytest
from yuriy.storage import Workspace, workspace_relative


@pytest.mark.parametrize('relative', [
    '.yuriy/workspace.lock',
    'projects/project-1/materials/file-1.blob',
    Path('.yuriy/workspace.lock'),
    PurePosixPath('.yuriy/workspace.lock'),
    PureWindowsPath('.yuriy/workspace.lock'),
    PureWindowsPath('projects/project-1/materials/file-1.blob'),
])
def test_relative_paths_before_platform_conversion(relative):
    expected = relative if isinstance(relative, str) else relative.as_posix()
    assert workspace_relative(relative).as_posix() == expected


@pytest.mark.parametrize('relative', [
    '../outside.txt', 'projects/../../outside.txt', '/absolute.txt',
    'C:/Windows/system.ini', 'C:relative.txt', 'materials/file.txt:secret',
    r'projects\raw-backslash.txt', r'\\server\share\file.txt', '//server/share/file.txt',
    PureWindowsPath('C:/Windows/system.ini'), PureWindowsPath('C:relative.txt'),
    PureWindowsPath('//server/share/file.txt'), PureWindowsPath('../outside.txt'),
    PureWindowsPath('/rooted.txt'), '', '.', 'a\x00b', b'projects/file.txt',
])
def test_unsafe_relative_inputs_rejected(relative):
    with pytest.raises(ValueError):
        workspace_relative(relative)


def test_original_windows_regression_with_pure_paths(tmp_path):
    relative = PureWindowsPath('.yuriy/workspace.lock')
    assert '\\' in str(relative)  # Original validator rejected this representation.
    workspace = Workspace(tmp_path/'Юрий с пробелами')
    try:
        result = workspace.safe(relative)
        assert result == workspace.root/'.yuriy'/'workspace.lock'
    finally:
        workspace.close()


@pytest.mark.skipif(os.name != 'nt', reason='Requires live Windows NTFS junction/reparse attributes')
def test_live_windows_junction_rejected(tmp_path):
    workspace = Workspace(tmp_path/'workspace')
    outside = tmp_path/'outside'; outside.mkdir()
    link = workspace.root/'projects'/'junction'
    try:
        made = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
        if made.returncode:
            pytest.skip('NTFS junction creation unavailable on this filesystem/account')
        with pytest.raises(ValueError):
            workspace.safe('projects/junction/outside.txt')
        assert not (outside/'outside.txt').exists()
    finally:
        if link.exists(): link.rmdir()  # Remove only the junction, never recurse into target.
        workspace.close()
