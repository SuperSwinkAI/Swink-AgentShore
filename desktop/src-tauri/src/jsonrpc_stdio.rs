use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const JSONRPC_VERSION: &str = "2.0";
pub const PARSE_ERROR: i32 = -32700;
pub const INVALID_REQUEST: i32 = -32600;
pub const METHOD_NOT_FOUND: i32 = -32601;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    pub id: Value,
    pub method: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JsonRpcError {
    pub code: i32,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    pub id: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

pub fn handshake_request(id: i64, client: &str, client_build_id: &str) -> JsonRpcRequest {
    JsonRpcRequest {
        jsonrpc: JSONRPC_VERSION.to_string(),
        id: Value::from(id),
        method: "app.handshake".to_string(),
        params: Some(serde_json::json!({
            "client": client,
            "client_build_id": client_build_id,
        })),
    }
}

pub fn encode_line<T: Serialize>(payload: &T) -> Result<String, serde_json::Error> {
    Ok(format!("{}\n", serde_json::to_string(payload)?))
}

pub fn decode_response_line(line: &str) -> Result<JsonRpcResponse, serde_json::Error> {
    serde_json::from_str(line.trim())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handshake_request_has_standard_envelope() {
        let req = handshake_request(7, "agentshore-desktop", "abc123");
        assert_eq!(req.jsonrpc, JSONRPC_VERSION);
        assert_eq!(req.id, Value::from(7));
        assert_eq!(req.method, "app.handshake");
        let params = req.params.expect("handshake params");
        assert_eq!(params["client"], "agentshore-desktop");
        assert_eq!(params["client_build_id"], "abc123");
    }

    #[test]
    fn encode_line_appends_newline() {
        let req = handshake_request(1, "agentshore-desktop", "build-1");
        let line = encode_line(&req).expect("encode request");
        assert!(line.ends_with('\n'));
        assert!(line.contains("\"jsonrpc\":\"2.0\""));
    }

    #[test]
    fn decode_response_line_parses_result_payload() {
        let line = r#"{"jsonrpc":"2.0","id":1,"result":{"protocol_version":1,"capabilities":["app.handshake"]}}"#;
        let response = decode_response_line(line).expect("decode response");
        assert_eq!(response.jsonrpc, JSONRPC_VERSION);
        assert_eq!(response.id, Value::from(1));
        assert!(response.error.is_none());
        assert_eq!(response.result.expect("result")["protocol_version"], 1);
    }

    #[test]
    fn decode_response_line_parses_error_payload() {
        let line = r#"{"jsonrpc":"2.0","id":7,"error":{"code":-32601,"message":"unknown method"}}"#;
        let response = decode_response_line(line).expect("decode response");
        assert!(response.result.is_none());
        let error = response.error.expect("error");
        assert_eq!(error.code, METHOD_NOT_FOUND);
        assert_eq!(error.message, "unknown method");
    }

    #[test]
    fn decode_response_line_rejects_invalid_json() {
        let err = decode_response_line("{not-json}").expect_err("should fail");
        assert!(err.is_syntax());
    }

    #[test]
    fn error_code_constants_match_jsonrpc_spec() {
        assert_eq!(PARSE_ERROR, -32700);
        assert_eq!(INVALID_REQUEST, -32600);
        assert_eq!(METHOD_NOT_FOUND, -32601);
    }
}
