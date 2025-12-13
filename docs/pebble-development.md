# Pebble Development Guide

This guide covers how to develop the Pebble app for PTV Notify using the Pebble SDK and the Pebble Tool extension in Antigravity IDE.

## Prerequisites

- Pebble SDK installed and configured
- Antigravity IDE with the **Pebble Tool** extension
- An example project is available at `Documents/cards-example` for reference

---

## Command Line Reference

### Building

```bash
pebble build
```

### Installing to Emulators

| Emulator | Device | Command |
|----------|--------|---------|
| `aplite` | Pebble Steel (B&W, 144×168) | `pebble install --emulator aplite` |
| `basalt` | Pebble Time (Color, 144×168) | `pebble install --emulator basalt` |
| `chalk` | Pebble Time Round (Color, 180×180) | `pebble install --emulator chalk` |
| `diorite` | Pebble 2 (B&W, 144×168) | `pebble install --emulator diorite` |

### Installing to Physical Watch

```bash
pebble install --phone <ip_address>
```

### Emulator Management

| Command | Description |
|---------|-------------|
| `pebble kill` | Kill the running emulator |
| `pebble wipe` | Reset the emulator to factory state |
| `pebble -h` | Show all available commands |

---

## Pebble Tool Extension (Recommended)

The **Pebble Tool** extension provides IDE integration for faster development.

### Keyboard Shortcuts

| Shortcut | Command | Description |
|----------|---------|-------------|
| `Ctrl+R B` | Build | Build the current project |
| `Ctrl+R Enter` | Build & Install | Clean build and install to emulator |
| `Ctrl+R E` | Install | Install on current emulator |
| `Ctrl+R I` | Temp Install | Install on a specific emulator |
| `Ctrl+R P` | Phone Install | Install on your phone |
| `Ctrl+R O` | Output | Open Pebble output window |
| `Ctrl+R C` | Custom Command | Run a custom pebble command |

### Command Palette

Access via `Ctrl+Shift+P`, then type `pbl:` to see all commands:

| Command | Description |
|---------|-------------|
| `pbl: Build current project` | Compile the app |
| `pbl: Build & Install to emulator` | Full rebuild and install |
| `pbl: Install on the current emulator` | Quick install |
| `pbl: Install on a specific emulator` | Choose emulator before install |
| `pbl: Install on your phone` | Deploy to physical watch |
| `pbl: Change the current emulator` | Switch default emulator |
| `pbl: Change the IP address of your phone` | Configure phone IP |
| `pbl: Generate new UUID` | Create a new app UUID |
| `pbl: Show output from last build` | View build logs |
| `pbl: Open Pebble output window` | Live output panel |
| `pbl: Run custom pebble command` | Run any pebble CLI command |
| `pbl: Check the Pebble SDK is ready` | Verify SDK setup |

---

## Development Workflow

### 1. Initial Setup

1. Open the command palette (`Ctrl+Shift+P`)
2. Run `pbl: Check the Pebble SDK is ready for pebble-tool`
3. Set your target emulator with `pbl: Change the current emulator`

### 2. Build & Test Cycle

**Fast iteration:**
1. Make code changes
2. Press `Ctrl+R Enter` (Build & Install)
3. View the app in the emulator

**Quick reinstall (no rebuild):**
- Press `Ctrl+R E` to reinstall the existing build

### 3. Testing on Multiple Platforms

Use `Ctrl+R I` to quickly test on different emulators:
- **aplite** - Test B&W display, memory constraints
- **basalt** - Test color display, animations
- **chalk** - Test round screen layout
- **diorite** - Test Pebble 2 features

### 4. Physical Watch Testing

1. Run `pbl: Change the IP address of your phone` 
2. Enter your phone's IP (find in Pebble app settings)
3. Press `Ctrl+R P` to install

---

## Platform Considerations

### Screen Sizes

| Platform | Resolution | Shape | Color |
|----------|------------|-------|-------|
| aplite | 144×168 | Rectangular | B&W |
| basalt | 144×168 | Rectangular | 64 colors |
| chalk | 180×180 | Round | 64 colors |
| diorite | 144×168 | Rectangular | B&W |

### Memory Limits

- **aplite/diorite**: ~24KB heap
- **basalt/chalk**: ~64KB heap

Test on aplite to catch memory issues early.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Build fails | Check `pbl: Show output from last build` |
| Emulator won't start | Run `pebble wipe` then try again |
| Phone install fails | Verify IP address, ensure phone and PC on same network |
| SDK not found | Run `pbl: Check the Pebble SDK is ready` |

---

## Next Steps

See the example app at `Documents/cards-example` for a working reference implementation.
