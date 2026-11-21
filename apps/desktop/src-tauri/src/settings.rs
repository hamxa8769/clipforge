//! Desktop settings, stored as plain JSON in the app-data dir.
//!
//! Deliberately **no secrets here**: API keys are stored only as *names*
//! (e.g. `YOUTUBE_API_KEY`). Their values live in a `.env` file next to
//! the server binary (loaded by `clipforge-server` on start) or in the
//! process environment. Never put a key value in this file.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PortMode {
    Auto,
    Manual,
}

impl Default for PortMode {
    fn default() -> Self {
        PortMode::Auto
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DesktopSettings {
    /// Override for the data folder. `None` = Tauri app-data dir.
    #[serde(default)]
    pub data_dir: Option<String>,
    /// `auto` = prefer port 8000, else any free port. `manual` = fixed port.
    #[serde(default)]
    pub port_mode: PortMode,
    /// Used only when `port_mode` is `manual`.
    #[serde(default = "default_manual_port")]
    pub manual_port: u16,
    /// Names of API keys the user has configured (values live in `.env`).
    #[serde(default)]
    pub api_key_names: Vec<String>,
}

fn default_manual_port() -> u16 {
    8000
}

impl Default for DesktopSettings {
    fn default() -> Self {
        DesktopSettings {
            data_dir: None,
            port_mode: PortMode::Auto,
            manual_port: default_manual_port(),
            api_key_names: Vec::new(),
        }
    }
}

impl DesktopSettings {
    pub fn settings_file(app_data_dir: &Path) -> PathBuf {
        app_data_dir.join("clipforge-desktop.json")
    }

    pub fn load(app_data_dir: &Path) -> Self {
        let path = Self::settings_file(app_data_dir);
        match fs::read_to_string(&path) {
            Ok(text) => serde_json::from_str(&text).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    pub fn save(&self, app_data_dir: &Path) -> std::io::Result<()> {
        let path = Self::settings_file(app_data_dir);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let text = serde_json::to_string_pretty(self)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        fs::write(path, text)
    }

    /// Effective data dir: the override if set, else the app-data dir.
    pub fn effective_data_dir(&self, app_data_dir: &Path) -> PathBuf {
        match &self.data_dir {
            Some(d) if !d.trim().is_empty() => PathBuf::from(d),
            _ => app_data_dir.to_path_buf(),
        }
    }

    /// Effective port: manual port when requested and valid, else `None`
    /// (caller picks a free port, preferring 8000).
    pub fn effective_manual_port(&self) -> Option<u16> {
        if self.port_mode == PortMode::Manual && (1024..=65535).contains(&self.manual_port) {
            Some(self.manual_port)
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp() -> PathBuf {
        let d = std::env::temp_dir().join("clipforge-test-settings");
        let _ = fs::create_dir_all(&d);
        d
    }

    #[test]
    fn round_trip() {
        let dir = tmp();
        let mut s = DesktopSettings::default();
        s.port_mode = PortMode::Manual;
        s.manual_port = 8123;
        s.api_key_names = vec!["YOUTUBE_API_KEY".into()];
        s.save(&dir).unwrap();
        assert_eq!(DesktopSettings::load(&dir), s);
        // The file must not contain a key *value* — only the name.
        let text = fs::read_to_string(DesktopSettings::settings_file(&dir)).unwrap();
        assert!(text.contains("YOUTUBE_API_KEY"));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn defaults_when_missing() {
        let dir = tmp();
        let _ = fs::remove_file(DesktopSettings::settings_file(&dir));
        assert_eq!(DesktopSettings::load(&dir), DesktopSettings::default());
    }

    #[test]
    fn manual_port_validation() {
        let mut s = DesktopSettings::default();
        s.port_mode = PortMode::Manual;
        s.manual_port = 80;
        assert_eq!(s.effective_manual_port(), None);
        s.manual_port = 8123;
        assert_eq!(s.effective_manual_port(), Some(8123));
        s.port_mode = PortMode::Auto;
        assert_eq!(s.effective_manual_port(), None);
    }
}
