# Per-wall dashboard setup

One env file per wall PC. Deploy code + generate `config.env` on the wall in one command.

## Easiest: interactive wizard (recommended)

Run the guided end-to-end wizard from your Mac — it collects the wall details and
passwords, writes `walls/<name>.env` (and optional `walls/_shared.env`), validates,
and deploys + starts the dashboard:

```bash
./setup.sh
```

It also detects existing wall configs so you can reconfigure or redeploy one.

## Manual quick start (new wall)

```bash
# 1. Optional: shared passwords for all walls
cp walls/_shared.env.example walls/_shared.env
# edit PANEL_SSH_PASS, DRIVE_PASSWORD

# 2. Create wall env
./new_wall.sh 10.91.0.99 in-new-wall XG-2532-3HLKT
# or: cp walls/in-qa-wall-001.env.example walls/in-new-wall.env

# 3. Edit wall file (IP, gateway serial, SBX USB)
nano walls/in-new-wall.env

# 4. Deploy + configure + bootstrap + install always-on systemd service
./setup_wall.sh walls/in-new-wall.env --start
```

## Files

| File | Purpose |
|------|---------|
| `walls/_shared.env` | Shared secrets (gitignored) |
| `walls/<name>.env` | Per-wall settings (gitignored) |
| `walls/_template.env` | Blank template |
| `walls/*.env.example` | Committed examples |

## Wall env → remote `config.env`

| Variable | Remote setting |
|----------|----------------|
| `WALL_IP` | `CAPTURE_SSH_HOST`, dashboard URL |
| `WALL_SSH_USER` | `CAPTURE_SSH_USER`, `/home/<user>/drive_automation` |
| `WALL_GATEWAY_SERIAL` | `GATEWAY_SERIAL` default |
| `GATEWAY_SERIALS` | Log capture dropdown |
| `PANEL_SSH_PASS` | Wall SSH for capture |
| `DRIVE_SBX_*` | FT232R USB device |

Preview generated config without deploying:

```bash
./scripts/render_config_env.sh walls/in-qa-wall-001.env.example
```

## Commands

```bash
./setup_wall.sh --list
./setup_wall.sh walls/in-qa-wall-001.env
./setup_wall.sh walls/in-qa-wall-001.env --force-config
./setup_wall.sh walls/in-qa-wall-001.env --code-only
./setup_wall.sh walls/in-qa-wall-001.env --start
```
