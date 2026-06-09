use agentshore_desktop_lib::github_multi::{handle_request, GitHubMultiError, HelperRequest};
use serde::Serialize;
use std::io::{Read, Write};

const SUCCESS: i32 = 0;
const PROTOCOL_ERROR: i32 = 2;

#[derive(Serialize)]
#[serde(untagged)]
enum HelperEnvelope {
    Ok {
        ok: bool,
        result: serde_json::Value,
    },
    Err {
        ok: bool,
        error: agentshore_desktop_lib::github_multi::HelperErrorBody,
    },
}

#[tokio::main]
async fn main() {
    let code = match run().await {
        Ok(()) => SUCCESS,
        Err(()) => PROTOCOL_ERROR,
    };
    std::process::exit(code);
}

async fn run() -> Result<(), ()> {
    let mut input = String::new();
    if std::io::stdin().read_to_string(&mut input).is_err() {
        write_error(GitHubMultiError::InvalidRequest(
            "could not read request from stdin".to_string(),
        ))?;
        return Err(());
    }
    let request = match serde_json::from_str::<HelperRequest>(&input) {
        Ok(request) => request,
        Err(err) => {
            write_error(GitHubMultiError::Json(err))?;
            return Err(());
        }
    };
    match handle_request(request).await {
        Ok(result) => {
            write_envelope(&HelperEnvelope::Ok { ok: true, result })?;
            Ok(())
        }
        Err(err) => {
            write_error(err)?;
            Err(())
        }
    }
}

fn write_error(err: GitHubMultiError) -> Result<(), ()> {
    write_envelope(&HelperEnvelope::Err {
        ok: false,
        error: err.body(),
    })
}

fn write_envelope(envelope: &HelperEnvelope) -> Result<(), ()> {
    let line = serde_json::to_string(envelope).map_err(|_| ())?;
    let mut stdout = std::io::stdout().lock();
    stdout.write_all(line.as_bytes()).map_err(|_| ())?;
    stdout.write_all(b"\n").map_err(|_| ())?;
    stdout.flush().map_err(|_| ())
}
