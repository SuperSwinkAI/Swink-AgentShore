use super::GitHubMultiError;

#[cfg(target_os = "windows")]
use std::ptr;

#[cfg(target_os = "windows")]
use windows_sys::Win32::Security::Credentials::{
    CredDeleteW, CredFree, CredReadW, CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE,
    CRED_TYPE_GENERIC,
};

#[cfg(target_os = "windows")]
use windows_sys::Win32::Foundation::GetLastError;

#[cfg(target_os = "windows")]
pub fn get_token(service: &str) -> Result<Option<String>, GitHubMultiError> {
    let target = wide_nul(service)?;
    let mut credential: *mut CREDENTIALW = ptr::null_mut();
    // SAFETY: `target` is NUL-terminated and lives through the call. The API
    // writes an owned CREDENTIALW pointer which must be released with CredFree.
    let ok = unsafe {
        CredReadW(
            target.as_ptr(),
            CRED_TYPE_GENERIC,
            0,
            &mut credential as *mut *mut CREDENTIALW,
        )
    };
    if ok == 0 {
        let code = unsafe { GetLastError() };
        if code == 1168 {
            return Ok(None);
        }
        return Err(GitHubMultiError::Credential(format!(
            "CredReadW failed with Windows error {code}"
        )));
    }
    if credential.is_null() {
        return Ok(None);
    }
    let result = unsafe {
        let cred = &*credential;
        let bytes =
            std::slice::from_raw_parts(cred.CredentialBlob, cred.CredentialBlobSize as usize);
        let token = String::from_utf8(bytes.to_vec()).map_err(|_| {
            GitHubMultiError::Credential("stored credential is not valid UTF-8".to_string())
        });
        CredFree(credential.cast());
        token
    }?;
    Ok(Some(result))
}

#[cfg(target_os = "windows")]
pub fn set_token(service: &str, token: &str) -> Result<(), GitHubMultiError> {
    let mut target = wide_nul(service)?;
    let mut username = wide_nul(service)?;
    let bytes = token.as_bytes();
    let credential = CREDENTIALW {
        Flags: 0,
        Type: CRED_TYPE_GENERIC,
        TargetName: target.as_mut_ptr(),
        Comment: ptr::null_mut(),
        LastWritten: Default::default(),
        CredentialBlobSize: bytes.len() as u32,
        CredentialBlob: bytes.as_ptr() as *mut u8,
        Persist: CRED_PERSIST_LOCAL_MACHINE,
        AttributeCount: 0,
        Attributes: ptr::null_mut(),
        TargetAlias: ptr::null_mut(),
        UserName: username.as_mut_ptr(),
    };
    // SAFETY: all pointers in CREDENTIALW reference buffers that live through
    // the call. The Credential Manager copies the blob before returning.
    let ok = unsafe { CredWriteW(&credential as *const CREDENTIALW, 0) };
    if ok == 0 {
        let code = unsafe { GetLastError() };
        return Err(GitHubMultiError::Credential(format!(
            "CredWriteW failed with Windows error {code}"
        )));
    }
    Ok(())
}

#[cfg(target_os = "windows")]
pub fn delete_token(service: &str) -> Result<bool, GitHubMultiError> {
    let target = wide_nul(service)?;
    // SAFETY: `target` is NUL-terminated and lives through the call.
    let ok = unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) };
    if ok == 0 {
        let code = unsafe { GetLastError() };
        if code == 1168 {
            return Ok(false);
        }
        return Err(GitHubMultiError::Credential(format!(
            "CredDeleteW failed with Windows error {code}"
        )));
    }
    Ok(true)
}

#[cfg(target_os = "windows")]
fn wide_nul(value: &str) -> Result<Vec<u16>, GitHubMultiError> {
    if value.encode_utf16().any(|unit| unit == 0) {
        return Err(GitHubMultiError::InvalidRequest(
            "value contains an embedded NUL".to_string(),
        ));
    }
    Ok(value.encode_utf16().chain(std::iter::once(0)).collect())
}

#[cfg(not(target_os = "windows"))]
pub fn get_token(_service: &str) -> Result<Option<String>, GitHubMultiError> {
    Err(GitHubMultiError::UnsupportedPlatform(
        "Windows Credential Manager is only available on Windows",
    ))
}

#[cfg(not(target_os = "windows"))]
pub fn set_token(_service: &str, _token: &str) -> Result<(), GitHubMultiError> {
    Err(GitHubMultiError::UnsupportedPlatform(
        "Windows Credential Manager is only available on Windows",
    ))
}

#[cfg(not(target_os = "windows"))]
pub fn delete_token(_service: &str) -> Result<bool, GitHubMultiError> {
    Err(GitHubMultiError::UnsupportedPlatform(
        "Windows Credential Manager is only available on Windows",
    ))
}

#[cfg(all(test, target_os = "windows"))]
mod tests {
    use super::*;

    #[test]
    fn credential_manager_round_trip() {
        let service = format!(
            "agentshore/test/{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        );
        set_token(&service, "test-token").expect("store token");
        assert_eq!(
            get_token(&service).expect("read token"),
            Some("test-token".to_string())
        );
        assert!(delete_token(&service).expect("delete token"));
        assert_eq!(get_token(&service).expect("read missing"), None);
    }
}
