//! One request on a holder's socket, answered with one response.

use std::path::Path;

use anyhow::{Context, Result, anyhow};
use holder_proto::{Request, Response};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

pub async fn request(socket: &Path, req: &Request) -> Result<Response> {
    let stream = UnixStream::connect(socket).await.with_context(|| format!("connecting to {}", socket.display()))?;
    let (read, mut write) = stream.into_split();
    let mut line = serde_json::to_vec(req)?;
    line.push(b'\n');
    write.write_all(&line).await?;
    let mut answer = String::new();
    BufReader::new(read).read_line(&mut answer).await?;
    if answer.is_empty() {
        return Err(anyhow!("the holder closed the connection"));
    }
    Ok(serde_json::from_str(&answer)?)
}
