//! Persisted UI preferences (theme, last-selected tab, onboarding flag,
//! window geometry) — the `tauri-plugin-store`-backed `ui-state.json` blob
//! and the Tauri commands the React shell uses to read/write it.

use crate::window::WindowState;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::Mutex;
use tauri::{AppHandle, Manager};
use tauri_plugin_store::StoreExt;

const UI_STATE_STORE_PATH: &str = "ui-state.json";
const UI_STATE_KEY: &str = "ui_state";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct UiState {
    pub theme: String,
    pub last_selected_tab: String,
    pub window: Option<WindowState>,
    // `#[serde(default)]` is load-bearing: a pre-existing `ui-state.json` lacks
    // this field, and without the default the whole struct would fail to
    // deserialize and silently reset theme/tab/window. `false` == carousel unseen.
    #[serde(default)]
    pub onboarding_completed: bool,
}

impl Default for UiState {
    fn default() -> Self {
        Self {
            theme: "system".to_string(),
            last_selected_tab: "home".to_string(),
            window: None,
            onboarding_completed: false,
        }
    }
}

#[derive(Default)]
pub struct UiStateHolder {
    pub state: Mutex<UiState>,
}

pub fn read_ui_state(app: &AppHandle) -> UiState {
    let store = match app.store(UI_STATE_STORE_PATH) {
        Ok(store) => store,
        Err(_) => return UiState::default(),
    };

    match store.get(UI_STATE_KEY) {
        Some(value) => serde_json::from_value::<UiState>(value).unwrap_or_default(),
        None => UiState::default(),
    }
}

pub fn persist_ui_state(app: &AppHandle, state: &UiState) -> Result<(), String> {
    let store = app.store(UI_STATE_STORE_PATH).map_err(|e| e.to_string())?;
    store.set(UI_STATE_KEY, json!(state));
    store.save().map_err(|e| e.to_string())
}

pub fn with_ui_state<R>(app: &AppHandle, f: impl FnOnce(&mut UiState) -> R) -> Result<R, String> {
    let holder = app.state::<UiStateHolder>();
    let mut guard = holder.state.lock().map_err(|e| e.to_string())?;
    Ok(f(&mut guard))
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn load_ui_state(app: AppHandle) -> Result<UiState, String> {
    let state = read_ui_state(&app);
    with_ui_state(&app, |current| {
        *current = state.clone();
    })?;
    Ok(state)
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn set_ui_theme(app: AppHandle, theme: String) -> Result<UiState, String> {
    let trimmed = theme.trim();
    if trimmed.is_empty() {
        return Err("theme must not be empty".to_string());
    }
    let next = with_ui_state(&app, |state| {
        state.theme = trimmed.to_string();
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn set_onboarding_completed(app: AppHandle, completed: bool) -> Result<UiState, String> {
    let next = with_ui_state(&app, |state| {
        state.onboarding_completed = completed;
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

#[cfg_attr(test, allow(dead_code))]
#[tauri::command]
pub fn set_last_selected_tab(app: AppHandle, tab: String) -> Result<UiState, String> {
    let trimmed = tab.trim();
    if trimmed.is_empty() {
        return Err("tab must not be empty".to_string());
    }
    let next = with_ui_state(&app, |state| {
        state.last_selected_tab = trimmed.to_string();
        state.clone()
    })?;
    persist_ui_state(&app, &next)?;
    Ok(next)
}

#[cfg(test)]
mod tests {
    use super::UiState;

    #[test]
    fn ui_state_defaults_match_shell_expectations() {
        let state = UiState::default();
        assert_eq!(state.theme, "system");
        assert_eq!(state.last_selected_tab, "home");
        assert!(state.window.is_none());
        assert!(!state.onboarding_completed);
    }

    #[test]
    fn ui_state_deserialize_invalid_payload_falls_back_to_default() {
        let parsed = serde_json::from_str::<UiState>("{\"theme\":123}");
        assert!(parsed.is_err());
    }

    #[test]
    fn ui_state_legacy_blob_without_onboarding_field_preserves_other_settings() {
        // A `ui-state.json` written before `onboarding_completed` existed must
        // still deserialize (via `#[serde(default)]`) rather than wiping the
        // user's theme / tab / window through the `unwrap_or_default()` path.
        let legacy = "{\"theme\":\"dark\",\"lastSelectedTab\":\"stats\",\"window\":null}";
        let parsed = serde_json::from_str::<UiState>(legacy).expect("legacy blob deserializes");
        assert_eq!(parsed.theme, "dark");
        assert_eq!(parsed.last_selected_tab, "stats");
        assert!(!parsed.onboarding_completed);
    }

    #[test]
    fn ui_state_round_trips_onboarding_completed() {
        let state = UiState {
            onboarding_completed: true,
            ..UiState::default()
        };
        let json = serde_json::to_string(&state).expect("serialize");
        let parsed = serde_json::from_str::<UiState>(&json).expect("deserialize");
        assert!(parsed.onboarding_completed);
    }
}
