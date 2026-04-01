// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
//! Lula guest agent — runs inside the Firecracker microVM.
//!
//! Listens for newline-delimited JSON command requests, executes them with
//! `tokio::process::Command`, and returns newline-delimited JSON responses.
//!
//! Transport:
//!   - Linux: AF_VSOCK on port `GUEST_AGENT_PORT` (default 52525).
//!   - Other platforms (dev/test): Unix domain socket at `/tmp/lula-agent.sock`.
//!
//! Protocol (one exchange per connection):
//!   Request:  `{"cmd":"cargo","args":["test","--quiet"],"cwd":"/workspace","env":{},"timeout_ms":30000}\n`
//!   Response: `{"ok":true,"exit_code":0,"stdout":"...","stderr":"...","timing_ms":1234}\n`

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    time::timeout,
};

// ---------------------------------------------------------------------------
// Protocol types
// ---------------------------------------------------------------------------

#[allow(dead_code)]
#[derive(Debug, Deserialize)]
struct CommandRequest {
    cmd: String,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    cwd: String,
    #[serde(default)]
    env: HashMap<String, String>,
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

#[allow(dead_code)]
fn default_timeout_ms() -> u64 {
    30_000
}

#[allow(dead_code)]
#[derive(Debug, Serialize)]
struct CommandResponse {
    ok: bool,
    exit_code: i32,
    stdout: String,
    stderr: String,
    timing_ms: u64,
}

// ---------------------------------------------------------------------------
// Request handler
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Guest command allowlist (defense-in-depth)
// ---------------------------------------------------------------------------

const ALLOWED_GUEST_COMMANDS: &[&str] = &[
    "python", "python3", "uv", "pip", "pip3", "cargo", "rustc", "node", "npm", "npx", "go", "git",
    "sh", "bash", "echo", "cat", "ls", "find", "grep", "sed", "awk", "mkdir", "cp", "mv", "rm",
    "touch", "true", "false", "pytest", "ruff", "mypy",
];

fn is_allowed_command(cmd: &str) -> bool {
    let base = std::path::Path::new(cmd).file_name().and_then(|n| n.to_str()).unwrap_or(cmd);
    ALLOWED_GUEST_COMMANDS.contains(&base)
}

// ---------------------------------------------------------------------------
// Request handler
// ---------------------------------------------------------------------------

#[allow(dead_code)]
async fn handle_request(req: CommandRequest) -> CommandResponse {
    let started = Instant::now();

    if !is_allowed_command(&req.cmd) {
        return CommandResponse {
            ok: false,
            exit_code: -1,
            stdout: String::new(),
            stderr: format!("command '{}' not in guest allowlist", req.cmd),
            timing_ms: started.elapsed().as_millis() as u64,
        };
    }

    let t = Duration::from_millis(req.timeout_ms);

    let mut cmd = tokio::process::Command::new(&req.cmd);
    cmd.args(&req.args);
    cmd.stdin(std::process::Stdio::null());
    cmd.stdout(std::process::Stdio::piped());
    cmd.stderr(std::process::Stdio::piped());
    cmd.env_clear();
    for (k, v) in &req.env {
        cmd.env(k, v);
    }
    if !req.cwd.is_empty() {
        cmd.current_dir(&req.cwd);
    }

    let child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            return CommandResponse {
                ok: false,
                exit_code: -1,
                stdout: String::new(),
                stderr: format!("spawn failed: {e}"),
                timing_ms: started.elapsed().as_millis() as u64,
            };
        }
    };

    match timeout(t, child.wait_with_output()).await {
        Ok(Ok(out)) => {
            let exit_code = out.status.code().unwrap_or(1);
            CommandResponse {
                ok: exit_code == 0,
                exit_code,
                stdout: String::from_utf8_lossy(&out.stdout).to_string(),
                stderr: String::from_utf8_lossy(&out.stderr).to_string(),
                timing_ms: started.elapsed().as_millis() as u64,
            }
        }
        Ok(Err(e)) => CommandResponse {
            ok: false,
            exit_code: -1,
            stdout: String::new(),
            stderr: format!("wait failed: {e}"),
            timing_ms: started.elapsed().as_millis() as u64,
        },
        Err(_) => CommandResponse {
            ok: false,
            exit_code: -1,
            stdout: String::new(),
            stderr: "command timed out".to_string(),
            timing_ms: started.elapsed().as_millis() as u64,
        },
    }
}

// ---------------------------------------------------------------------------
// Connection handler — reads one request, writes one response
// ---------------------------------------------------------------------------

#[allow(dead_code)]
async fn handle_connection<R, W>(reader: R, mut writer: W)
where
    R: tokio::io::AsyncRead + Unpin,
    W: tokio::io::AsyncWrite + Unpin,
{
    let mut buf_reader = BufReader::new(reader);
    let mut line = String::new();

    if let Ok(n) = buf_reader.read_line(&mut line).await {
        if n == 0 {
            return; // EOF
        }
        // Enforce a 1 MiB request body limit.
        const MAX_REQUEST_BYTES: usize = 1024 * 1024;
        if line.len() > MAX_REQUEST_BYTES {
            let resp = CommandResponse {
                ok: false,
                exit_code: -1,
                stdout: String::new(),
                stderr: format!(
                    "request body too large: {} bytes (limit {} bytes)",
                    line.len(),
                    MAX_REQUEST_BYTES
                ),
                timing_ms: 0,
            };
            if let Ok(mut json_bytes) = serde_json::to_vec(&resp) {
                json_bytes.push(b'\n');
                let _ = writer.write_all(&json_bytes).await;
            }
            return;
        }
        let resp = match serde_json::from_str::<CommandRequest>(line.trim()) {
            Ok(req) => handle_request(req).await,
            Err(e) => CommandResponse {
                ok: false,
                exit_code: -1,
                stdout: String::new(),
                stderr: format!("json parse error: {e}"),
                timing_ms: 0,
            },
        };
        if let Ok(mut json_bytes) = serde_json::to_vec(&resp) {
            json_bytes.push(b'\n');
            let _ = writer.write_all(&json_bytes).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Listener — vsock on Linux, Unix domain socket elsewhere
// ---------------------------------------------------------------------------

#[cfg(target_os = "linux")]
mod listener {
    use std::os::unix::io::FromRawFd;

    use tokio::net::UnixListener;

    use super::handle_connection;

    /// Vsock port the agent listens on.
    fn agent_port() -> u32 {
        std::env::var("GUEST_AGENT_PORT").ok().and_then(|v| v.trim().parse().ok()).unwrap_or(52525)
    }

    pub async fn run() -> std::io::Result<()> {
        let port = agent_port();

        // Try to bind AF_VSOCK; fall back to Unix domain socket if vsock is
        // not available (e.g., inside a container during integration testing).
        let fd = unsafe { libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };

        if fd < 0 {
            eprintln!(
                "vsock socket() failed ({errno}); falling back to UDS",
                errno = std::io::Error::last_os_error()
            );
            return run_uds().await;
        }

        // SAFETY: sockaddr_vm is a plain C struct; zeroing all bytes is valid.
        // Using mem::zeroed() + field assignment avoids struct-literal breakage
        // across libc versions where svm_flags may or may not exist.
        let mut addr: libc::sockaddr_vm = unsafe { std::mem::zeroed() };
        addr.svm_family = libc::AF_VSOCK as libc::sa_family_t;
        addr.svm_port = port;
        // VMADDR_CID_HOST = 2: accept connections only from the host
        // hypervisor, not from other VMs sharing the same host.
        addr.svm_cid = 2; // VMADDR_CID_HOST

        let ret = unsafe {
            libc::bind(
                fd,
                &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
                std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
            )
        };
        if ret < 0 {
            unsafe { libc::close(fd) };
            eprintln!("vsock bind() failed; falling back to UDS");
            return run_uds().await;
        }

        let ret = unsafe { libc::listen(fd, 128) };
        if ret < 0 {
            unsafe { libc::close(fd) };
            return Err(std::io::Error::last_os_error());
        }

        // Set non-blocking so tokio can drive it.
        let ret = unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) };
        if ret < 0 {
            unsafe { libc::close(fd) };
            return Err(std::io::Error::last_os_error());
        }

        eprintln!("lula-guest-agent listening on vsock port {port}");

        // SAFETY: fd is a valid AF_VSOCK SOCK_STREAM listening socket in
        // non-blocking mode.  We wrap it as std::os::unix::net::UnixListener
        // (not std::net::TcpListener) to avoid the TcpListener type-contract
        // violation (TcpListener requires AF_INET/AF_INET6).  On Linux, both
        // AF_UNIX and AF_VSOCK are SOCK_STREAM and tokio drives them identically
        // via epoll.
        let std_listener = unsafe { std::os::unix::net::UnixListener::from_raw_fd(fd) };
        std_listener.set_nonblocking(true)?;
        let listener = UnixListener::from_std(std_listener)?;

        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    tokio::spawn(async move {
                        let (r, w) = stream.into_split();
                        handle_connection(r, w).await;
                    });
                }
                Err(e) => {
                    eprintln!("accept error: {e}");
                }
            }
        }
    }

    async fn run_uds() -> std::io::Result<()> {
        let path = uds_path();
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        eprintln!("lula-guest-agent listening on UDS {path}");
        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    tokio::spawn(async move {
                        let (r, w) = stream.into_split();
                        handle_connection(r, w).await;
                    });
                }
                Err(e) => {
                    eprintln!("accept error: {e}");
                }
            }
        }
    }

    fn uds_path() -> String {
        std::env::var("GUEST_AGENT_SOCK").unwrap_or_else(|_| "/tmp/lula-agent.sock".to_string())
    }
}

// On Unix systems that are not Linux (e.g., macOS), use a Unix domain socket.
#[cfg(all(unix, not(target_os = "linux")))]
mod listener {
    use tokio::net::UnixListener;

    use super::handle_connection;

    fn uds_path() -> String {
        std::env::var("GUEST_AGENT_SOCK").unwrap_or_else(|_| "/tmp/lula-agent.sock".to_string())
    }

    pub async fn run() -> std::io::Result<()> {
        let path = uds_path();
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path)?;
        eprintln!("lula-guest-agent listening on UDS {path}");
        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    tokio::spawn(async move {
                        let (r, w) = stream.into_split();
                        handle_connection(r, w).await;
                    });
                }
                Err(e) => {
                    eprintln!("accept error: {e}");
                }
            }
        }
    }
}

// On Windows the guest agent cannot run; provide a stub that always errors.
#[cfg(not(unix))]
mod listener {
    pub async fn run() -> std::io::Result<()> {
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "lula-guest-agent is not supported on this platform (Linux only)",
        ))
    }
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

#[tokio::main]
async fn main() -> std::io::Result<()> {
    listener::run().await
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_handle_request_echo() {
        #[cfg(unix)]
        {
            let req = CommandRequest {
                cmd: "echo".to_string(),
                args: vec!["hello".to_string()],
                cwd: String::new(),
                env: HashMap::new(),
                timeout_ms: 5000,
            };
            let resp = handle_request(req).await;
            assert!(resp.ok);
            assert_eq!(resp.exit_code, 0);
            assert!(resp.stdout.trim() == "hello");
        }
    }

    #[tokio::test]
    async fn test_handle_request_nonexistent_command() {
        // `lula_nonexistent_cmd_xyz` is not in the allowlist, so the guest
        // agent should reject it before attempting to spawn.
        let req = CommandRequest {
            cmd: "lula_nonexistent_cmd_xyz".to_string(),
            args: vec![],
            cwd: String::new(),
            env: HashMap::new(),
            timeout_ms: 5000,
        };
        let resp = handle_request(req).await;
        assert!(!resp.ok);
        assert_eq!(resp.exit_code, -1);
        assert!(resp.stderr.contains("lula_nonexistent_cmd_xyz"));
    }

    #[tokio::test]
    async fn test_handle_request_disallowed_command_rejected() {
        let req = CommandRequest {
            cmd: "curl".to_string(),
            args: vec!["https://example.com".to_string()],
            cwd: String::new(),
            env: HashMap::new(),
            timeout_ms: 5000,
        };
        let resp = handle_request(req).await;
        assert!(!resp.ok);
        assert_eq!(resp.exit_code, -1);
        assert!(resp.stderr.contains("not in guest allowlist"));
    }

    #[test]
    fn test_is_allowed_command_base_name() {
        assert!(is_allowed_command("python3"));
        assert!(is_allowed_command("/usr/bin/python3"));
        assert!(is_allowed_command("cargo"));
        assert!(!is_allowed_command("curl"));
        assert!(!is_allowed_command("/usr/bin/curl"));
        assert!(!is_allowed_command("nc"));
    }

    #[tokio::test]
    async fn test_handle_connection_json_roundtrip() {
        #[cfg(unix)]
        {
            let input = b"{\"cmd\":\"echo\",\"args\":[\"roundtrip\"],\"cwd\":\"\",\"env\":{},\"timeout_ms\":5000}\n";
            let mut output = Vec::new();
            handle_connection(input.as_ref(), &mut output).await;
            let resp: serde_json::Value = serde_json::from_slice(&output).unwrap();
            assert_eq!(resp["ok"], true);
            assert_eq!(resp["exit_code"], 0);
        }
    }

    #[tokio::test]
    async fn test_handle_connection_bad_json() {
        let input = b"not valid json\n";
        let mut output = Vec::new();
        handle_connection(input.as_ref(), &mut output).await;
        let resp: serde_json::Value = serde_json::from_slice(&output).unwrap();
        assert_eq!(resp["ok"], false);
        assert!(resp["stderr"].as_str().unwrap().contains("json parse error"));
    }
}
