// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Christian Meurer — https://github.com/christianmeurer/Lula
//! Vsock client for communicating with the Firecracker guest agent.
//!
//! On Linux, uses `AF_VSOCK` sockets to reach the guest agent running inside
//! the microVM.  On other platforms (Windows / macOS development), falls back
//! to a Unix domain socket at the path supplied by the caller — this enables
//! full unit-test coverage without a real Firecracker instance.
//!
//! # Protocol
//! Each connection carries exactly one request/response exchange.
//! Both are newline-terminated JSON objects:
//!
//! ```text
//! Request:  {"cmd":"cargo","args":["test"],"cwd":"/workspace","env":{},"timeout_ms":30000}\n
//! Response: {"ok":true,"exit_code":0,"stdout":"...","stderr":"...","timing_ms":1234}\n
//! ```

use std::collections::HashMap;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use crate::errors::ApiError;

// ---------------------------------------------------------------------------
// Public protocol types
// ---------------------------------------------------------------------------

/// A command request sent to the guest agent.
#[derive(Debug, Serialize)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub struct GuestCommandRequest {
    pub cmd: String,
    pub args: Vec<String>,
    pub cwd: String,
    pub env: HashMap<String, String>,
    pub timeout_ms: u64,
}

/// A command response received from the guest agent.
#[derive(Debug, Deserialize)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub struct GuestCommandResponse {
    pub ok: bool,
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub timing_ms: u64,
}

// ---------------------------------------------------------------------------
// Internal wire helpers
// ---------------------------------------------------------------------------

/// Write `req` as newline-terminated JSON to `writer`, then read a newline-
/// terminated JSON response from `reader`.
#[allow(dead_code)]
async fn exchange<R, W>(
    reader: R,
    mut writer: W,
    req: &GuestCommandRequest,
) -> Result<GuestCommandResponse, ApiError>
where
    R: tokio::io::AsyncRead + Unpin,
    W: tokio::io::AsyncWrite + Unpin,
{
    let mut json_bytes =
        serde_json::to_vec(req).map_err(|e| ApiError::Other(anyhow::anyhow!("vsock serialize: {e}")))?;
    json_bytes.push(b'\n');

    writer
        .write_all(&json_bytes)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock write: {e}")))?;

    writer
        .flush()
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock flush: {e}")))?;

    let mut line = String::new();
    let mut buf_reader = BufReader::new(reader);
    buf_reader
        .read_line(&mut line)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock read: {e}")))?;

    serde_json::from_str(line.trim())
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock deserialize: {e} — raw: {line}")))
}

// ---------------------------------------------------------------------------
// Linux vsock transport
// ---------------------------------------------------------------------------

/// Send a command to the guest agent over an `AF_VSOCK` socket.
///
/// `cid` is the vsock Context ID assigned to the guest (Firecracker uses 3 for
/// the first VM).  `port` is the port the guest agent listens on (default
/// 52525).
///
/// This function is only available on Linux; non-Linux callers receive a
/// [`ApiError::BadRequest`] with a descriptive message.
#[cfg(target_os = "linux")]
pub async fn send_guest_command(
    cid: u32,
    port: u32,
    req: &GuestCommandRequest,
    timeout: Duration,
) -> Result<GuestCommandResponse, ApiError> {
    use std::os::unix::io::{FromRawFd, IntoRawFd};

    // Create an AF_VSOCK stream socket.
    let fd = unsafe {
        libc::socket(
            libc::AF_VSOCK,
            libc::SOCK_STREAM | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock socket() failed: {}",
            std::io::Error::last_os_error()
        )));
    }

    // Set non-blocking before connect so tokio can await it.
    let ret = unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) };
    if ret < 0 {
        unsafe { libc::close(fd) };
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock fcntl O_NONBLOCK failed: {}",
            std::io::Error::last_os_error()
        )));
    }

    let addr = libc::sockaddr_vm {
        svm_family: libc::AF_VSOCK as libc::sa_family_t,
        svm_reserved1: 0,
        svm_port: port,
        svm_cid: cid,
        svm_flags: 0,
        svm_zero: [0u8; 4],
    };

    let ret = unsafe {
        libc::connect(
            fd,
            &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
        )
    };

    // EINPROGRESS is expected for non-blocking connect.
    if ret < 0 {
        let errno = unsafe { *libc::__errno_location() };
        if errno != libc::EINPROGRESS {
            unsafe { libc::close(fd) };
            return Err(ApiError::Other(anyhow::anyhow!(
                "vsock connect() failed: {}",
                std::io::Error::from_raw_os_error(errno)
            )));
        }
    }

    // Wrap the raw fd into a std TcpStream (same layout for raw fd ops),
    // then into a tokio stream.
    let std_stream = unsafe { std::net::TcpStream::from_raw_fd(fd) };
    let stream = tokio::net::TcpStream::from_std(std_stream)
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock tokio wrap: {e}")))?;

    // Wait for the non-blocking connect to complete.
    stream
        .writable()
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock writable await: {e}")))?;

    // Check for connect error via SO_ERROR.
    let so_error = stream
        .take_error()
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock take_error: {e}")))?;
    if let Some(e) = so_error {
        return Err(ApiError::Other(anyhow::anyhow!("vsock connect error: {e}")));
    }

    let (r, w) = stream.into_split();

    tokio::time::timeout(timeout, exchange(r, w, req))
        .await
        .map_err(|_| ApiError::Other(anyhow::anyhow!("vsock exchange timed out")))?
}

/// Non-Linux stub — always returns a [`ApiError::BadRequest`].
#[cfg(not(target_os = "linux"))]
#[allow(dead_code)]
pub async fn send_guest_command(
    _cid: u32,
    _port: u32,
    _req: &GuestCommandRequest,
    _timeout: Duration,
) -> Result<GuestCommandResponse, ApiError> {
    Err(ApiError::BadRequest(
        "MicroVmEphemeral sandbox requires Linux (vsock not available on this platform)".into(),
    ))
}

// ---------------------------------------------------------------------------
// Unix domain socket transport (dev/test, non-Linux)
// ---------------------------------------------------------------------------

/// Send a command to the guest agent over a Unix domain socket.
///
/// This is the testing transport used on macOS/Windows development machines.
/// The guest agent must also be started with `GUEST_AGENT_SOCK=<socket_path>`.
#[cfg(unix)]
pub async fn send_guest_command_via_uds(
    socket_path: &str,
    req: &GuestCommandRequest,
    timeout: Duration,
) -> Result<GuestCommandResponse, ApiError> {
    let stream = tokio::net::UnixStream::connect(socket_path)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("uds connect {socket_path}: {e}")))?;

    let (r, w) = stream.into_split();

    tokio::time::timeout(timeout, exchange(r, w, req))
        .await
        .map_err(|_| ApiError::Other(anyhow::anyhow!("uds exchange timed out")))?
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_guest_command_request_serializes() {
        let req = GuestCommandRequest {
            cmd: "cargo".to_string(),
            args: vec!["test".to_string(), "--quiet".to_string()],
            cwd: "/workspace".to_string(),
            env: {
                let mut m = HashMap::new();
                m.insert("RUST_LOG".to_string(), "info".to_string());
                m
            },
            timeout_ms: 30_000,
        };
        let json = serde_json::to_value(&req).unwrap();
        assert_eq!(json["cmd"], "cargo");
        assert_eq!(json["args"][0], "test");
        assert_eq!(json["args"][1], "--quiet");
        assert_eq!(json["cwd"], "/workspace");
        assert_eq!(json["env"]["RUST_LOG"], "info");
        assert_eq!(json["timeout_ms"], 30_000u64);
    }

    #[test]
    fn test_guest_command_response_deserializes() {
        let json = r#"{"ok":true,"exit_code":0,"stdout":"test output","stderr":"","timing_ms":1234}"#;
        let resp: GuestCommandResponse = serde_json::from_str(json).unwrap();
        assert!(resp.ok);
        assert_eq!(resp.exit_code, 0);
        assert_eq!(resp.stdout, "test output");
        assert_eq!(resp.stderr, "");
        assert_eq!(resp.timing_ms, 1234);
    }

    #[test]
    fn test_guest_command_response_error_deserializes() {
        let json = r#"{"ok":false,"exit_code":-1,"stdout":"","stderr":"spawn failed: No such file or directory","timing_ms":0}"#;
        let resp: GuestCommandResponse = serde_json::from_str(json).unwrap();
        assert!(!resp.ok);
        assert_eq!(resp.exit_code, -1);
        assert!(resp.stderr.contains("spawn failed"));
    }

    #[cfg(not(target_os = "linux"))]
    #[tokio::test]
    async fn test_send_guest_command_returns_platform_error_on_non_linux() {
        let req = GuestCommandRequest {
            cmd: "echo".to_string(),
            args: vec!["hello".to_string()],
            cwd: String::new(),
            env: HashMap::new(),
            timeout_ms: 5000,
        };
        let result =
            send_guest_command(3, 52525, &req, Duration::from_secs(5)).await;
        assert!(
            matches!(result, Err(ApiError::BadRequest(ref msg)) if msg.contains("Linux")),
            "expected BadRequest platform error, got: {result:?}"
        );
    }

    /// Roundtrip test using a local Unix domain socket.
    ///
    /// Spawns a minimal echo server, sends one request, verifies the response.
    /// Gated with `#[cfg(unix)]` — not available on Windows.
    #[cfg(unix)]
    #[tokio::test]
    async fn test_send_via_uds_roundtrip() {
        use tokio::net::UnixListener;

        let tmp = tempfile::tempdir().unwrap();
        let sock_path = tmp.path().join("test-agent.sock");
        let sock_path_str = sock_path.to_str().unwrap().to_string();

        // Minimal echo server: reads one line, writes a canned OK response.
        let listener = UnixListener::bind(&sock_path).unwrap();
        let server = tokio::spawn(async move {
            if let Ok((stream, _)) = listener.accept().await {
                use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
                let (r, mut w) = stream.into_split();
                let mut br = BufReader::new(r);
                let mut line = String::new();
                br.read_line(&mut line).await.unwrap();
                // Verify we got a valid request.
                let req: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
                assert_eq!(req["cmd"], "echo");
                let resp = r#"{"ok":true,"exit_code":0,"stdout":"hello\n","stderr":"","timing_ms":1}"#;
                w.write_all(resp.as_bytes()).await.unwrap();
                w.write_all(b"\n").await.unwrap();
            }
        });

        // Give the server a moment to bind.
        tokio::time::sleep(Duration::from_millis(10)).await;

        let req = GuestCommandRequest {
            cmd: "echo".to_string(),
            args: vec!["hello".to_string()],
            cwd: String::new(),
            env: HashMap::new(),
            timeout_ms: 5000,
        };
        let result =
            send_guest_command_via_uds(&sock_path_str, &req, Duration::from_secs(5)).await;
        server.await.unwrap();

        let resp = result.expect("uds roundtrip failed");
        assert!(resp.ok);
        assert_eq!(resp.exit_code, 0);
    }
}
