//! Removing something for good. It is renamed into the trash first: the trash is on the same volume, so that's instant
//! and nobody sees a half-deleted folder. Then it is deleted file by file, counting the space freed.

use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static MOVED: AtomicU64 = AtomicU64::new(0);

/// What deleting a tree did.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct Deleted {
    pub bytes: u64,
    /// Entries that couldn't be deleted (another owner's files, most likely).
    pub failed: u64,
}

/// A new name in `trash` for `target`.
pub fn trash_path(target: &Path, trash: &Path) -> PathBuf {
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    trash.join(format!("{}-{}-{}-{name}", chrono::Utc::now().format("%Y%m%dT%H%M%S"), std::process::id(),
                       MOVED.fetch_add(1, Ordering::Relaxed)))
}

/// Move `target` into `trash` under a new name, and return where it went.
pub fn to_trash(target: &Path, trash: &Path) -> std::io::Result<PathBuf> {
    plain_dir(trash)?;
    let dest = trash_path(target, trash);
    std::fs::rename(target, &dest)?;
    Ok(dest)
}

/// Make sure `dir` is a folder of its own (made if missing), not a symlink to somewhere else.
pub fn plain_dir(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let meta = std::fs::symlink_metadata(dir)?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidInput, format!("{} isn't a plain folder", dir.display())));
    }
    Ok(())
}

/// Delete `path` and everything under it without following symlinks. A folder of our own that we may not write to is
/// opened up first (some tools make their caches read-only). A file's space counts only when its last link goes.
pub fn delete_tree(path: &Path) -> Deleted {
    let mut d = Deleted::default();
    // SAFETY: geteuid has no preconditions.
    let me = unsafe { libc::geteuid() };
    delete(path, me, &mut d);
    d
}

/// Whether `path` is gone.
fn delete(path: &Path, me: u32, d: &mut Deleted) -> bool {
    let meta = match std::fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return true,
        Err(_) => {
            d.failed += 1;
            return false;
        }
    };
    if meta.is_dir() {
        if meta.uid() == me && meta.mode() & 0o700 != 0o700 {
            let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(meta.mode() | 0o700));
        }
        let mut emptied = true;
        match std::fs::read_dir(path) {
            Ok(entries) => {
                for e in entries {
                    match e {
                        Ok(e) => emptied &= delete(&e.path(), me, d),
                        Err(_) => emptied = false,
                    }
                }
            }
            Err(_) => emptied = false,
        }
        if emptied && std::fs::remove_dir(path).is_ok() {
            d.bytes += meta.blocks() * 512;
            return true;
        }
        if emptied {
            d.failed += 1;
        }
        false
    } else {
        match std::fs::remove_file(path) {
            Ok(()) => {
                if meta.nlink() <= 1 {
                    d.bytes += meta.blocks() * 512;
                }
                true
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => true,
            Err(_) => {
                d.failed += 1;
                false
            }
        }
    }
}

/// The space `path` takes, without following symlinks (for dry runs).
pub fn tree_bytes(path: &Path) -> u64 {
    let Ok(meta) = std::fs::symlink_metadata(path) else { return 0 };
    let own = if meta.is_dir() || meta.nlink() <= 1 { meta.blocks() * 512 } else { 0 };
    if !meta.is_dir() {
        return own;
    }
    own + std::fs::read_dir(path).map(|es| es.flatten().map(|e| tree_bytes(&e.path())).sum()).unwrap_or(0)
}

/// Whether `path` is plain (absolute, no `.` or `..`) and lies at least `depth` levels inside `root`, with no symlink
/// on the way from `root` to it, the path itself included.
pub fn inside(path: &Path, root: &Path, depth: usize) -> bool {
    let plain = path.is_absolute() && !path.components().any(|c| matches!(c, Component::ParentDir | Component::CurDir));
    let Ok(rest) = path.strip_prefix(root) else { return false };
    plain && rest.components().count() >= depth && {
        let mut at = root.to_path_buf();
        rest.components().all(|c| {
            at.push(c);
            std::fs::symlink_metadata(&at).is_ok_and(|m| !m.file_type().is_symlink())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn to_trash_moves_and_names_uniquely() {
        let t = tempfile::tempdir().unwrap();
        let trash = t.path().join("trash");
        for _ in 0..2 {
            fs::create_dir_all(t.path().join("f/node_modules/x")).unwrap();
            let went = to_trash(&t.path().join("f/node_modules"), &trash).unwrap();
            assert!(went.join("x").is_dir() && went.starts_with(&trash));
            assert!(!t.path().join("f/node_modules").exists());
        }
        assert_eq!(fs::read_dir(&trash).unwrap().count(), 2);
    }

    #[test]
    fn delete_tree_counts_bytes_and_leaves_symlink_targets_alone() {
        let t = tempfile::tempdir().unwrap();
        let (gone, kept) = (t.path().join("gone"), t.path().join("kept"));
        fs::create_dir_all(gone.join("a/b")).unwrap();
        fs::create_dir_all(&kept).unwrap();
        fs::write(gone.join("a/b/f"), vec![1u8; 100_000]).unwrap();
        fs::write(kept.join("mine"), "x").unwrap();
        std::os::unix::fs::symlink(&kept, gone.join("link")).unwrap();
        let expected = tree_bytes(&gone);
        let d = delete_tree(&gone);
        assert_eq!(d, Deleted { bytes: expected, failed: 0 });
        assert!(d.bytes >= 100_000);
        assert!(!gone.exists() && kept.join("mine").exists());
    }

    #[test]
    fn delete_tree_opens_up_read_only_folders_of_its_own() {
        let t = tempfile::tempdir().unwrap();
        let dir = t.path().join("cache/pkg");
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("f"), "x").unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o555)).unwrap();
        assert_eq!(delete_tree(&t.path().join("cache")).failed, 0);
        assert!(!t.path().join("cache").exists());
    }

    #[test]
    fn inside_needs_a_plain_path_deep_enough_with_no_symlinks() {
        let t = tempfile::tempdir().unwrap();
        let scratch = t.path().join("scratch");
        fs::create_dir_all(scratch.join("V-1/app/node_modules")).unwrap();
        fs::create_dir_all(t.path().join("memory/node_modules")).unwrap();
        std::os::unix::fs::symlink(t.path().join("memory"), scratch.join("V-1/link")).unwrap();
        assert!(inside(&scratch.join("V-1/app/node_modules"), &scratch, 2));
        assert!(inside(&scratch.join("V-1/app"), &scratch, 2));
        assert!(!inside(&scratch.join("V-1"), &scratch, 2), "a conversation's folder itself");
        assert!(!inside(&scratch, &scratch, 1));
        assert!(!inside(&scratch.join("V-1/link/node_modules"), &scratch, 2), "through a symlink");
        assert!(!inside(&scratch.join("V-1/../../memory"), &scratch, 2));
        assert!(!inside(&t.path().join("memory/node_modules"), &scratch, 1));
        assert!(!inside(Path::new("scratch/V-1/app"), Path::new("scratch"), 2));
    }

    #[test]
    fn a_trash_that_is_a_symlink_is_refused() {
        let t = tempfile::tempdir().unwrap();
        fs::create_dir_all(t.path().join("memory")).unwrap();
        fs::write(t.path().join("memory/note.md"), "keep").unwrap();
        std::os::unix::fs::symlink(t.path().join("memory"), t.path().join("trash")).unwrap();
        fs::create_dir_all(t.path().join("f/node_modules")).unwrap();
        assert!(to_trash(&t.path().join("f/node_modules"), &t.path().join("trash")).is_err());
        assert!(t.path().join("f/node_modules").exists() && t.path().join("memory/note.md").exists());
    }
}
