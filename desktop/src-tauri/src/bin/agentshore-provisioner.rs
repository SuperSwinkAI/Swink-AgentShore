mod agentshore_provisioner;

use agentshore_provisioner::*;
use std::env;
use std::ffi::OsString;
use std::fs;
use std::time::Duration;

fn main() {
    let code = match run() {
        Ok(()) => SUCCESS,
        Err(err) => {
            eprintln!("{}", err.message);
            err.code
        }
    };
    std::process::exit(code);
}

fn run() -> ProvisionResult<()> {
    let mut args: Vec<OsString> = env::args_os().skip(1).collect();
    if args.is_empty() {
        return Err(usage());
    }
    let command = args.remove(0);
    match command.to_string_lossy().as_ref() {
        "sidecar" => run_sidecar(args),
        "cli" => run_cli(args),
        "timelapse" => run_timelapse(args),
        _ => Err(usage()),
    }
}

fn usage() -> ProvisionError {
    ProvisionError::new(
        INVALID_ARGS,
        "usage: agentshore-provisioner <sidecar|cli|timelapse> [--wheel PATH] [--uv PATH]",
    )
}

fn run_sidecar(args: Vec<OsString>) -> ProvisionResult<()> {
    let parsed = ParsedArgs::parse(args)?;
    let wheel = parsed.required_path("--wheel")?;
    let uv = parsed.required_path("--uv")?;
    validate_file(&wheel, "--wheel")?;
    validate_file(&uv, "--uv")?;

    let logger = Logger::open("Provisioning-AgentShore-Desktop-sidecar")?;
    logger.line("==> Provisioning AgentShore Desktop sidecar");
    logger.line(format!("wheel: {}", wheel.display()));
    logger.line(format!("uv: {}", uv.display()));

    let root = agentshore_programdata_root();
    let venv = managed_venv_path();
    let bin = machine_bin_path();
    let runtime = runtime_dir();
    fs::create_dir_all(&root).map_err(|err| {
        ProvisionError::new(
            PROCESS_OR_SWAP_FAILURE,
            format!("create {}: {err}", root.display()),
        )
    })?;
    fs::create_dir_all(&bin).map_err(|err| {
        ProvisionError::new(
            PROCESS_OR_SWAP_FAILURE,
            format!("create {}: {err}", bin.display()),
        )
    })?;
    fs::create_dir_all(&runtime).map_err(|err| {
        ProvisionError::new(
            PROCESS_OR_SWAP_FAILURE,
            format!("create {}: {err}", runtime.display()),
        )
    })?;
    grant_modify(&agentshore_programdata_root().join("install-logs"), &logger)?;
    grant_modify(&runtime, &logger)?;

    cleanup_legacy_layout(&logger);
    stop_recorded_runtime_processes(&logger)?;

    replace_venv_with_rollback(&venv, &logger, || {
        logger.line("==> Creating managed venv");
        run_required(
            "uv venv",
            UV_VENV_FAILURE,
            &uv,
            &[
                os("--native-tls"),
                os("venv"),
                os("--python"),
                os("3.12"),
                venv.clone().into_os_string(),
            ],
            UV_VENV_TIMEOUT,
            &logger,
        )?;

        let venv_python = managed_venv_python_path();
        validate_file(&venv_python, "venv python").map_err(|err| {
            ProvisionError::new(
                UV_VENV_FAILURE,
                format!(
                    "venv python missing at {}: {}",
                    venv_python.display(),
                    err.message
                ),
            )
        })?;

        logger.line("==> Installing agentshore wheel");
        run_required(
            "uv pip install",
            WHEEL_INSTALL_FAILURE,
            &uv,
            &[
                os("--native-tls"),
                os("pip"),
                os("install"),
                os("--python"),
                venv_python.clone().into_os_string(),
                wheel.clone().into_os_string(),
            ],
            WHEEL_INSTALL_TIMEOUT,
            &logger,
        )?;

        logger.line("==> Verifying agentshore.sidecar import");
        run_required(
            "sidecar import",
            SIDECAR_IMPORT_FAILURE,
            &venv_python,
            &[
                os("-c"),
                os("import agentshore.sidecar; print('agentshore.sidecar OK')"),
            ],
            SIDECAR_IMPORT_TIMEOUT,
            &logger,
        )?;

        logger.line("==> Provisioning bd dependency");
        let code = format!(
            "from pathlib import Path\nfrom agentshore.beads.setup import provision_bd\npath = provision_bd(assume_yes=True, dest_dir=Path({}))\nraise SystemExit(0 if path else 1)\n",
            python_string_literal(&bin)
        );
        run_required(
            "bd provisioning",
            BD_PROVISION_FAILURE,
            &venv_python,
            &[os("-c"), OsString::from(code)],
            BD_PROVISION_TIMEOUT,
            &logger,
        )?;

        grant_read_execute(&venv, &logger)?;
        grant_read_execute(&bin, &logger)?;
        Ok(())
    })?;

    logger.line("==> Installed managed sidecar");
    Ok(())
}

fn run_cli(args: Vec<OsString>) -> ProvisionResult<()> {
    let parsed = ParsedArgs::parse(args)?;
    let wheel = parsed.required_path("--wheel")?;
    let uv = parsed.required_path("--uv")?;
    validate_file(&wheel, "--wheel")?;
    validate_file(&uv, "--uv")?;

    let logger = Logger::open("Installing-AgentShore-CLI")?;
    logger.line("==> Installing AgentShore CLI");
    let wheel_uri = wheel_uri(&wheel);
    let package = OsString::from(format!("agentshore @ {wheel_uri}"));
    run_required(
        "uv tool install",
        CLI_FAILURE,
        &uv,
        &[
            os("tool"),
            os("install"),
            os("--native-tls"),
            os("--force"),
            os("--reinstall"),
            os("--python"),
            os("3.12"),
            package,
        ],
        CLI_TIMEOUT,
        &logger,
    )?;
    let _ = run_logged(
        "uv tool update-shell",
        &uv,
        &[os("tool"), os("update-shell")],
        Duration::from_secs(120),
        &logger,
    );
    logger.line("==> Installed AgentShore CLI");
    Ok(())
}

fn run_timelapse(_args: Vec<OsString>) -> ProvisionResult<()> {
    let logger = Logger::open("Provisioning-Timelapse-Capture")?;
    logger.line("==> Provisioning Timelapse Capture");
    let python = managed_venv_python_path();
    validate_file(&python, "managed venv python").map_err(|err| {
        ProvisionError::new(
            TIMELAPSE_FAILURE,
            format!("managed venv missing: {}", err.message),
        )
    })?;
    let code = "import asyncio, sys\nfrom agentshore.timelapse.setup import install_timelapse\nresult = asyncio.run(install_timelapse())\nprint(result.message)\nsys.exit(0 if result.success else 1)\n";
    run_required(
        "timelapse provisioning",
        TIMELAPSE_FAILURE,
        &python,
        &[os("-c"), os(code)],
        TIMELAPSE_TIMEOUT,
        &logger,
    )?;
    logger.line("==> Installed Timelapse Capture");
    Ok(())
}
