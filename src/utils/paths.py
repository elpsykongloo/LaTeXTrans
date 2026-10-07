"""Project boundary checks shared by readers, writers and archive extraction."""
import os
import stat
from pathlib import Path


def is_project_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    # st_reparse_tag is available on Windows since Python 3.8;
    # Path.is_junction() only appeared in Python 3.12.
    return getattr(info, "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def project_path(root, candidate, *, writing=False) -> Path:
    """Reject paths outside a project and paths traversing links or junctions."""
    root_path = Path(os.path.abspath(root))
    path = Path(os.path.abspath(candidate))
    try:
        relative = path.relative_to(root_path)
        path.resolve().relative_to(root_path.resolve())
    except ValueError:
        raise ValueError(f"Path escapes project directory: {candidate}") from None
    cursor = root_path
    for part in ("", *relative.parts):
        if part:
            cursor /= part
        if is_project_link(cursor):
            raise ValueError(f"Project path traverses a link: {cursor}")
    if writing and path.is_file() and path.stat().st_nlink > 1:
        raise ValueError(f"Refusing to overwrite a hard-linked file: {path}")
    return path


def extract_zip(zip_ref, target):
    for member in zip_ref.infolist():
        project_path(target, Path(target) / member.filename, writing=True)
        mode = member.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"Archive links are unsupported: {member.filename}")
    zip_ref.extractall(target)


def validate_project_tree(root, *, writing=False) -> None:
    """Preflight an existing tree without descending into links."""
    project_path(root, root, writing=writing)
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in (*subdirs, *files):
            project_path(root, Path(directory) / name, writing=writing)


def extract_tar(tar_ref, target):
    for member in tar_ref.getmembers():
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Archive links and special files are unsupported: {member.name}")
        project_path(target, Path(target) / member.name, writing=True)
    tar_ref.extractall(target)
