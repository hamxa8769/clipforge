//! FFmpeg presence check.
//!
//! Clipping needs FFmpeg; the rest of ClipForge (review, SEO, scheduling)
//! does not. The app therefore never blocks on a missing FFmpeg — it
//! reports it and offers a "continue without clipping" path.

use serde::Serialize;
use std::process::Command;
#[cfg(windows)]
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize)]
pub struct FfmpegStatus {
    pub available: bool,
    pub version: Option<String>,
    pub path: Option<String>,
}

/// Parse the first line of `ffmpeg -version`, e.g.
/// `ffmpeg version 6.1.1-... Copyright ...` -> `Some("6.1.1-...")`.
pub fn parse_ffmpeg_version(first_line: &str) -> Option<String> {
    let rest = first_line.strip_prefix("ffmpeg version ")?;
    Some(rest.split_whitespace().next()?.to_string())
}

fn run_version(path: &str) -> Option<String> {
    let out = Command::new(path).arg("-version").output().ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let first = text.lines().next()?;
    parse_ffmpeg_version(first)
}

/// Common Windows install locations (checked after PATH).
#[cfg(windows)]
fn windows_candidates() -> Vec<PathBuf> {
    let mut out = Vec::new();
    // One-click install (v0.4.1): <data_dir>/ffmpeg/bin/ffmpeg.exe, where
    // the data dir is %APPDATA%/com.clipforge.app. Checked first so the
    // install the app itself performed is found even before a logoff
    // (the user-PATH edit only reaches new processes).
    if let Ok(appdata) = std::env::var("APPDATA") {
        out.push(
            PathBuf::from(appdata)
                .join("com.clipforge.app/ffmpeg/bin/ffmpeg.exe"),
        );
    }
    if let Ok(pf) = std::env::var("ProgramFiles") {
        out.push(PathBuf::from(&pf).join("ffmpeg/bin/ffmpeg.exe"));
        out.push(PathBuf::from(pf).join("Gyan/FFmpeg/bin/ffmpeg.exe"));
    }
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        out.push(
            PathBuf::from(local)
                .join("Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-7.1.1-full_build/bin/ffmpeg.exe"),
        );
    }
    out.push(PathBuf::from(r"C:\ffmpeg\bin\ffmpeg.exe"));
    out
}

/// Check PATH first, then (Windows) common install locations.
pub fn check_ffmpeg() -> FfmpegStatus {
    if let Some(v) = run_version("ffmpeg") {
        return FfmpegStatus {
            available: true,
            version: Some(v),
            path: Some("ffmpeg".into()),
        };
    }
    #[cfg(windows)]
    {
        for cand in windows_candidates() {
            let s = cand.to_string_lossy().into_owned();
            if cand.is_file() {
                if let Some(v) = run_version(&s) {
                    return FfmpegStatus {
                        available: true,
                        version: Some(v),
                        path: Some(s),
                    };
                }
            }
        }
    }
    FfmpegStatus {
        available: false,
        version: None,
        path: None,
    }
}

pub const FFMPEG_DOWNLOAD_URL: &str = "https://ffmpeg.org/download.html";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_version_line() {
        assert_eq!(
            parse_ffmpeg_version("ffmpeg version 6.1.1-essentials_build-www.gyan.dev Copyright (c) 2000-2023"),
            Some("6.1.1-essentials_build-www.gyan.dev".into())
        );
        assert_eq!(parse_ffmpeg_version("ffmpeg version 7.1"), Some("7.1".into()));
        assert_eq!(parse_ffmpeg_version("not ffmpeg"), None);
        assert_eq!(parse_ffmpeg_version("ffmpeg version"), None);
    }

    #[test]
    fn check_returns_a_struct() {
        // In this VM ffmpeg IS installed, so this also verifies the happy path.
        let st = check_ffmpeg();
        assert!(st.available);
        assert!(st.version.is_some());
    }
}
