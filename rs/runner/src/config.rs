use std::path::{Path, PathBuf};

use globset::{Glob, GlobSet, GlobSetBuilder};

#[derive(Clone, Debug)]
pub struct RunnerConfig {
    pub root_dir: PathBuf,
    pub allow_read: GlobSet,
    pub allow_write: GlobSet,
}

impl RunnerConfig {
    pub fn new(root_dir: impl AsRef<Path>) -> anyhow::Result<Self> {
        let root_dir = root_dir.as_ref().canonicalize()?;

        let mut allow_read = GlobSetBuilder::new();
        allow_read.add(Glob::new("**")?);
        let allow_read = allow_read.build()?;

        let mut allow_write = GlobSetBuilder::new();
        allow_write.add(Glob::new("**")?);
        let allow_write = allow_write.build()?;

        Ok(Self {
            root_dir,
            allow_read,
            allow_write,
        })
    }

    pub fn can_read(&self, rel: &str) -> bool {
        self.allow_read.is_match(rel)
    }

    pub fn can_write(&self, rel: &str) -> bool {
        self.allow_write.is_match(rel)
    }
}
