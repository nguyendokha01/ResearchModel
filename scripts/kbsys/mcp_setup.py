"""Connection snippets for MCP clients, with paths of this installation (the folder can be copied anywhere).

`kb.py mcp-config` prints them; `kb.py mcp-config --install <client>` merges the server into that client's
own config file after saving a backup next to it. Nothing is installed without the explicit flag.
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from . import config

NAME = "biomedical-kb"


def server_command(tools=None, memory=False):
    """`tools="brainstorm"` limits the server to the brainstorm toolset; `memory` switches AN-20 memory on (never for a
    client that may only retrieve, e.g. Hermes or the generic snippet)."""
    venv = config.ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = venv if venv.is_file() else Path(sys.executable)
    flags = (["--tools", tools] if tools and tools != "all" else []) + (["--memory"] if memory else [])
    return str(python), [str(config.ROOT / "scripts" / "kb.py"), "mcp", *flags]


def _json_entry(tools=None, memory=False):
    command, args = server_command(tools, memory)
    return {"command": command, "args": args, "env": {"PYTHONUTF8": "1"}}


def config_paths():
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    return {
        "claude-desktop": appdata / "Claude" / "claude_desktop_config.json" if os.name == "nt"
        else home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex") / "config.toml",
        "gemini-cli": home / ".gemini" / "settings.json",
        "cursor": home / ".cursor" / "mcp.json",
    }


def snippets(tools=None):
    command, args = server_command(tools)
    entry = {"mcpServers": {NAME: _json_entry(tools)}}
    memory_args = server_command(tools, True)[1]   # claude-desktop and codex only: they keep the researcher's memory
    toml_args = ", ".join(json.dumps(a) for a in memory_args)
    paths = config_paths()
    quoted = " ".join(f'"{a}"' for a in [command, *args])
    return {
        "claude-desktop": {"file": str(paths["claude-desktop"]), "add": {"mcpServers": {NAME: _json_entry(tools, True)}}},
        "claude-code": {"run": f"claude mcp add {NAME} -e PYTHONUTF8=1 -- {quoted}"},
        "codex": {"file": str(paths["codex"]),
                  "add": f'[mcp_servers.{NAME}]\ncommand = {json.dumps(command)}\nargs = [{toml_args}]\n'
                         f'env = {{ PYTHONUTF8 = "1" }}\n'},
        "gemini-cli": {"file": str(paths["gemini-cli"]), "add": entry},
        "cursor": {"file": str(paths["cursor"]), "add": entry},
        "generic": {"transport": "stdio", "command": command, "args": args, "env": {"PYTHONUTF8": "1"}},
    }


def _backup(path):
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(path.name + f".bak-{stamp}")
        shutil.copy2(path, backup)
        return str(backup)
    return None


def install(client, tools=None):
    """Merge this server into one client's config file (backup first). Returns a short report."""
    paths = config_paths()
    if client in ("claude-desktop", "gemini-cli", "cursor"):
        path = paths[client]
        data = {}
        if path.exists():
            try:  # utf-8-sig: Notepad may have saved the file with a BOM
                data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"{path} không phải JSON hợp lệ ({e}); sửa hoặc đổi tên file đó rồi chạy lại. "
                                 "Không thay đổi gì.") from None
            if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
                raise ValueError(f"{path}: cấu trúc lạ (cần {{\"mcpServers\": {{...}}}}); không thay đổi gì.")
        backup = _backup(path)
        data.setdefault("mcpServers", {})[NAME] = _json_entry(tools, client == "claude-desktop")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return {"client": client, "file": str(path), "backup": backup}
    if client == "codex":
        path = paths["codex"]
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if f"[mcp_servers.{NAME}]" in text:
            return {"client": client, "file": str(path), "note": "already configured; edit that section by hand"}
        backup = _backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.rstrip() + ("\n\n" if text.strip() else "") + snippets(tools)["codex"]["add"],
                        encoding="utf-8", newline="\n")
        return {"client": client, "file": str(path), "backup": backup}
    raise ValueError(f"--install supports claude-desktop, codex, gemini-cli, cursor (claude-code: run the printed "
                     f"command); got {client!r}")


def report_text(tools=None):
    s = snippets(tools)
    lines = ["Kết nối kho tri thức với ứng dụng AI qua MCP (chạy trên máy, không mở cổng mạng).",
             "Ứng dụng AI tự bật `kb.py mcp` khi cần. Sau khi thêm, khởi động lại ứng dụng đó.", ""]
    for client in ("claude-desktop", "claude-code", "codex", "gemini-cli", "cursor", "generic"):
        item = s[client]
        lines.append(f"== {client}")
        if "file" in item:
            lines.append(f"   File: {item['file']}")
        if "run" in item:
            lines.append(f"   Chạy lệnh: {item['run']}")
        if "add" in item:
            add = item["add"] if isinstance(item["add"], str) else json.dumps(item["add"], ensure_ascii=False, indent=2)
            lines.append("   Thêm:\n" + "\n".join("     " + x for x in add.splitlines()))
        if client == "generic":
            lines.append("   (Hermes hoặc ứng dụng khác: stdio) " + json.dumps(item, ensure_ascii=False))
            lines.append("   Hermes dùng mô hình đám mây (vd. qua 9router): mỗi câu hỏi gửi trích dẫn và hình từ kho tới nhà cung "
                         "cấp đó, không rõ là ai; chỉ dùng để hỏi đáp, không dùng cho phân tích bài hàng loạt.")   # O1/Q6
        lines.append("")
    lines.append("Tự thêm vào file cấu hình (có sao lưu): kb.py mcp-config --install claude-desktop|codex|gemini-cli|cursor")
    return "\n".join(lines)
