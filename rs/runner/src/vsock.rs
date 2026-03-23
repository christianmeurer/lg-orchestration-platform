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
    let mut json_bytes = serde_json::to_vec(req)
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock serialize: {e}")))?;
    json_bytes.push(b'\n');

    writer
        .write_all(&json_bytes)
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock write: {e}")))?;

    writer.flush().await.map_err(|e| ApiError::Other(anyhow::anyhow!("vsock flush: {e}")))?;

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
//
// # Why raw `libc` fd instead of tokio's built-in stream types?
//
// `AF_VSOCK` is a Linux-specific address family for hypervisor ↔ guest
// communication.  Neither `tokio::net::TcpStream` (AF_INET/AF_INET6) nor
// `tokio::net::UnixStream` (AF_UNIX) support it — tokio has no native
// AF_VSOCK abstraction (as of tokio 1.x).  We therefore:
//
//   1. Create the socket with `libc::socket(AF_VSOCK, SOCK_STREAM, 0)`.
//   2. Perform the non-blocking connect dance manually.
//   3. Wrap the resulting fd into `tokio::net::UnixStream` for async I/O
//      (see the SAFETY comment at the `from_raw_fd` call for why
//      `UnixStream` — not `TcpStream` — is the correct wrapper type).
//
// # `#[cfg(target_os = "linux")]` gate
//
// `AF_VSOCK` only exists on Linux (added in kernel 3.9).  The entire
// function is gated behind `#[cfg(target_os = "linux")]`; non-Linux
// targets get a compile-time stub that returns `ApiError::BadRequest`.
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
    use std::os::unix::io::{AsRawFd, FromRawFd};

    // Create a raw AF_VSOCK stream socket via libc.  We must go through libc
    // because tokio (and the Rust stdlib) have no AF_VSOCK-aware socket
    // constructor.  SOCK_CLOEXEC ensures the fd is not leaked to child
    // processes spawned by the runner.
    let fd = unsafe { libc::socket(libc::AF_VSOCK, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock socket() failed: {}",
            std::io::Error::last_os_error()
        )));
    }

    // The fd MUST be set to O_NONBLOCK *before* calling connect().  On a
    // non-blocking socket, connect() returns immediately with errno
    // EINPROGRESS while the kernel completes the three-way handshake in the
    // background.  This is essential because we are inside a tokio async
    // task — a blocking connect() would stall the entire executor thread.
    let ret = unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) };
    if ret < 0 {
        unsafe { libc::close(fd) };
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock fcntl O_NONBLOCK failed: {}",
            std::io::Error::last_os_error()
        )));
    }

    // SAFETY: sockaddr_vm is a plain C struct; zeroing all bytes is valid.
    // Using mem::zeroed() + field assignment avoids struct-literal breakage
    // across libc versions where svm_flags may or may not exist.
    let mut addr: libc::sockaddr_vm = unsafe { std::mem::zeroed() };
    addr.svm_family = libc::AF_VSOCK as libc::sa_family_t;
    addr.svm_port = port;
    addr.svm_cid = cid;

    let ret = unsafe {
        libc::connect(
            fd,
            &addr as *const libc::sockaddr_vm as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_vm>() as libc::socklen_t,
        )
    };

    // EINPROGRESS is the *expected* return for a non-blocking connect: it
    // means the kernel accepted the request and is completing the handshake
    // asynchronously.  Any other errno is a real failure (e.g. ENODEV when
    // the vsock transport is not loaded).
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

    // Convert the raw fd into a tokio-managed async stream.
    //
    // SAFETY invariants for `FromRawFd::from_raw_fd` + `from_std`:
    //   1. `fd` is a valid, open, non-blocking SOCK_STREAM file descriptor.
    //   2. Ownership is transferred — no other code will close this fd.
    //   3. The fd has not yet been registered with any other async reactor.
    //
    // We wrap the fd in `std::os::unix::net::UnixStream` (AF_UNIX wrapper)
    // rather than `std::net::TcpStream` (AF_INET/6 wrapper) because:
    //   - `TcpStream`'s type contract explicitly requires an AF_INET or
    //     AF_INET6 fd; violating it is undefined behaviour.
    //   - `UnixStream` wraps a generic SOCK_STREAM fd and tokio drives it
    //     via epoll readiness — the kernel address family is irrelevant to
    //     epoll, so AF_VSOCK fds work correctly through this path.
    let std_stream = unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) };
    let stream = tokio::net::UnixStream::from_std(std_stream)
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock tokio wrap: {e}")))?;

    // Wait for the non-blocking connect to complete.  `writable()` resolves
    // once epoll signals the fd is write-ready, which coincides with the
    // connect handshake finishing (successfully or not).
    stream
        .writable()
        .await
        .map_err(|e| ApiError::Other(anyhow::anyhow!("vsock writable await: {e}")))?;

    // Check for connect error via SO_ERROR.  tokio::net::UnixStream does not
    // expose take_error(), so we call getsockopt(SO_ERROR) directly.
    // A non-zero SO_ERROR means the connect handshake failed (e.g. the guest
    // agent is not listening on the requested CID:port).
    let mut so_err: libc::c_int = 0;
    let mut so_err_len = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
    let gso_ret = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_ERROR,
            &mut so_err as *mut libc::c_int as *mut libc::c_void,
            &mut so_err_len,
        )
    };
    if gso_ret < 0 {
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock getsockopt(SO_ERROR) failed: {}",
            std::io::Error::last_os_error()
        )));
    }
    if so_err != 0 {
        return Err(ApiError::Other(anyhow::anyhow!(
            "vsock connect error: {}",
            std::io::Error::from_raw_os_error(so_err)
        )));
    }

    let (r, w) = stream.into_split();

    tokio::time::timeout(timeout, exchange(r, w, req))
        .await
        .map_err(|_| ApiError::Other(anyhow::anyhow!("vsock exchange timed out")))?
}

/// Non-Linux stub — always returns a [`ApiError::BadRequest`].
///
/// `AF_VSOCK` is a Linux-only address family (kernel 3.9+); there is no
/// equivalent on macOS or Windows.  This stub lets the crate compile on all
/// platforms while clearly signalling that vsock communication requires Linux.
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
///
/// Only called from `#[cfg(test)]` — suppress dead_code warning.
#[cfg(unix)]
#[allow(dead_code)]
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
        let json =
            r#"{"ok":true,"exit_code":0,"stdout":"test output","stderr":"","timing_ms":1234}"#;
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
        let result = send_guest_command(3, 52525, &req, Duration::from_secs(5)).await;
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
                let resp =
                    r#"{"ok":true,"exit_code":0,"stdout":"hello\n","stderr":"","timing_ms":1}"#;
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
        let result = send_guest_command_via_uds(&sock_path_str, &req, Duration::from_secs(5)).await;
        server.await.unwrap();

        let resp = result.expect("uds roundtrip failed");
        assert!(resp.ok);
        assert_eq!(resp.exit_code, 0);
    }
}
