"""Minimal HTTP server for the Chess Rocket web dashboard.

Serves dashboard.html and JSON API endpoints for live game data.
Launch: uv run python scripts/dashboard_server.py [--port 8088]
"""

from __future__ import annotations

import argparse
import http.server
import importlib.util
import json
import os
import re
import sys
import subprocess
import threading
import tempfile
from pathlib import Path

import chess

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"

# The dashboard is a standalone tutor session.  It loads the same implementation
# used by the MCP server, but does not require an MCP client or console to be
# running.
_SERVER_SPEC = importlib.util.spec_from_file_location(
    "chess_rocket_dashboard_mcp", _PROJECT_ROOT / "mcp-server" / "server.py"
)
if _SERVER_SPEC is None or _SERVER_SPEC.loader is None:
    raise RuntimeError("Unable to load the Chess Rocket game service")
_game_service = importlib.util.module_from_spec(_SERVER_SPEC)
sys.modules[_SERVER_SPEC.name] = _game_service
_SERVER_SPEC.loader.exec_module(_game_service)

_active_game_id: str | None = None
_state_lock = threading.RLock()
_chat_history: list[dict[str, str]] = []

_COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "demo_title": {"type": "string"},
        "demo_moves": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
    },
    "required": ["message", "demo_title", "demo_moves"],
    "additionalProperties": False,
}


def _full_state(game_id: str) -> dict:
    """Return the dashboard's full state contract, not the minified MCP result."""
    game = _game_service._games.get(game_id)
    if game is None:
        return {"error": f"Game not found: {game_id}"}
    return _game_service._build_game_state(game_id, game)


def _model_reply(user_message: str, state: dict, move_context: dict | None = None) -> dict:
    """Ask the locally authenticated Codex CLI for tutor language.

    The browser never receives credentials.  ``codex exec`` uses the same local
    Codex login and configuration as the user's normal console, while the
    prompt contains the authoritative position/evaluation from this dashboard
    session.  Chess state is deliberately not delegated to the model.
    """

    position = {
        "fen": state.get("fen"),
        "turn": "White" if state.get("fen", "").split(" ")[1:2] == ["w"] else "Black",
        "moves": state.get("move_list", []),
        "player_color": state.get("player_color"),
        "target_elo": state.get("target_elo"),
    }
    if move_context:
        position["last_move_evaluation"] = move_context

    history = "\n".join(
        f"{item['role'].title()}: {item['content']}" for item in _chat_history[-10:]
    )
    prompt = (
        "You are the Chess Rocket tutor. Reply directly to the player in a warm, "
        "concise way. Adjust vocabulary to their Elo. Explain one actionable idea "
        "and use the engine evaluation when supplied. The position and chess state "
        "below are authoritative. Do not call tools, do not change files, and do "
        "not claim to have changed the board. If a concrete variation or idea is "
        "helpful, include up to 8 legal SAN moves starting from the exact current "
        "position in demo_moves; otherwise use an empty array. Return the requested "
        "JSON object only.\n\n"
        f"Position: {json.dumps(position, ensure_ascii=False)}\n"
        f"Conversation:\n{history or '(none)'}\n"
        f"Player request: {user_message}"
    )
    with tempfile.TemporaryDirectory(prefix="chess-rocket-codex-") as temp_dir:
        schema_path = Path(temp_dir) / "coach-schema.json"
        answer_path = Path(temp_dir) / "coach-answer.json"
        schema_path.write_text(json.dumps(_COACH_SCHEMA), encoding="utf-8")
        command = [
        os.environ.get("CODEX_BIN", "codex"),
        "-a", "never",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "-C", str(_PROJECT_ROOT),
        "-s", "read-only",
        "--output-schema", str(schema_path),
        "--output-last-message", str(answer_path),
        "-",
        ]
        try:
            result = subprocess.run(
                command,
                input=prompt,
                cwd=_PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("CODEX_TIMEOUT", "120")),
                check=False,
            )
            raw = answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
            try:
                lesson = json.loads(raw)
            except json.JSONDecodeError:
                lesson = None
                for line in result.stdout.splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    item = event.get("item", {})
                    if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                        try:
                            lesson = json.loads(item.get("text", ""))
                        except json.JSONDecodeError:
                            lesson = {"message": item.get("text", ""), "demo_title": "", "demo_moves": []}
                        break
            if result.returncode != 0 and not lesson:
                detail = result.stderr.strip() or f"codex exited with status {result.returncode}"
                lesson = {"message": f"Local Codex is unavailable: {detail}", "demo_title": "", "demo_moves": []}
            if not isinstance(lesson, dict):
                lesson = {"message": "I couldn't produce a coaching note.", "demo_title": "", "demo_moves": []}
            return {
                "message": str(lesson.get("message", "I couldn't produce a coaching note.")),
                "demo_title": str(lesson.get("demo_title", "")),
                "demo_moves": [str(move) for move in lesson.get("demo_moves", [])][:8],
            }
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            return {"message": f"Local Codex is unavailable: {exc}", "demo_title": "", "demo_moves": []}


def _validate_demo_line(game_id: str, lesson: dict, state: dict) -> dict:
    """Validate Codex's SAN line and convert it to overlay-friendly UCI moves."""
    game = _game_service._games.get(game_id)
    moves = lesson.get("demo_moves", [])
    if game is None or not isinstance(moves, list):
        lesson["demo_moves"] = []
        return lesson
    board = game["board"].copy()
    validated = []
    for candidate in moves:
        candidate = re.sub(r"^(?:\d+\.(?:\.\.)?|\.\.\.)", "", str(candidate)).strip()
        try:
            move = board.parse_san(candidate)
        except (ValueError, _game_service.chess.InvalidMoveError, _game_service.chess.IllegalMoveError, _game_service.chess.AmbiguousMoveError):
            break
        validated.append({"san": board.san(move), "uci": move.uci(), "from": move.uci()[:2], "to": move.uci()[2:4]})
        board.push(move)
    lesson["demo_moves"] = validated
    return lesson


def _json_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 64_000:
        raise ValueError("Request body is too large")
    raw = handler.rfile.read(length) if length else b"{}"
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """Serves dashboard HTML and JSON API endpoints."""

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "/index.html":
            self._serve_file(_DASHBOARD_HTML, "text/html; charset=utf-8")
        elif self.path == "/api/game":
            if _active_game_id:
                with _state_lock:
                    self._serve_payload(_full_state(_active_game_id))
            else:
                # Do not display a stale game written by the separate MCP
                # console process. The dashboard owns its own game session.
                self._serve_payload(None)
        elif self.path == "/api/progress":
            self._serve_json(_DATA_DIR / "progress.json")
        elif self.path == "/api/chat/history":
            self._serve_payload({"messages": _chat_history})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        global _active_game_id
        try:
            payload = _json_body(self)
            with _state_lock:
                if self.path == "/api/game/new":
                    target_elo = int(payload.get("target_elo", 800))
                    player_color = payload.get("player_color", "white")
                    if player_color not in ("white", "black"):
                        raise ValueError("player_color must be white or black")
                    result = _game_service.new_game(target_elo, player_color)
                    _active_game_id = result["game_id"]
                    _chat_history.clear()
                    state = _full_state(_active_game_id)
                    self._serve_payload({"state": state, "message": "Game started."})
                    return

                if self.path == "/api/game/move":
                    if not _active_game_id:
                        raise ValueError("Start a game first")
                    game = _game_service._games.get(_active_game_id)
                    if game is None:
                        raise ValueError("Active game is no longer available")
                    board = game["board"]
                    if board.is_game_over():
                        raise ValueError("The game is already over")
                    player_turn = "white" if board.turn else "black"
                    if player_turn != game["player_color"]:
                        raise ValueError("It is the coach's turn")
                    uci = str(payload.get("move", ""))
                    # The board sends four-character UCI for ordinary moves.
                    # Default promotions to a queen until a promotion picker is
                    # added to the frontend.
                    if len(uci) == 4 and board.piece_at(chess.parse_square(uci[:2])):
                        piece = board.piece_at(chess.parse_square(uci[:2]))
                        target_rank = uci[3]
                        if piece.piece_type == chess.PAWN and target_rank in ("1", "8"):
                            uci += "q"
                    chess_move = chess.Move.from_uci(uci)
                    if chess_move not in board.legal_moves:
                        raise ValueError("Illegal move")
                    san = board.san(chess_move)
                    evaluation = _game_service.evaluate_move(_active_game_id, san)
                    if "error" in evaluation:
                        raise ValueError(evaluation["error"])
                    _game_service.make_move(_active_game_id, san)
                    state = _full_state(_active_game_id)
                    if not state["is_game_over"]:
                        _game_service.engine_move(_active_game_id)
                        state = _full_state(_active_game_id)
                    coach_prompt = (
                        f"The player played {san}. Give a brief coaching explanation, "
                        "then mention what to look for in the next position."
                    )
                    lesson = _model_reply(coach_prompt, state, {
                        "move": san,
                        "classification": evaluation.get("classification"),
                        "cp_loss": evaluation.get("cp_loss"),
                        "best_move": evaluation.get("best_move_san"),
                    })
                    lesson = _validate_demo_line(_active_game_id, lesson, state)
                    _chat_history.extend([
                        {"role": "user", "content": f"I played {san}."},
                        {"role": "assistant", "content": lesson["message"]},
                    ])
                    self._serve_payload({"state": state, "evaluation": evaluation, "reply": lesson["message"], "lesson": lesson})
                    return

                if self.path == "/api/game/engine-move":
                    if not _active_game_id:
                        raise ValueError("Start a game first")
                    game = _game_service._games.get(_active_game_id)
                    if game is None or game["board"].is_game_over():
                        raise ValueError("No engine move is available")
                    _game_service.engine_move(_active_game_id)
                    self._serve_payload({"state": _full_state(_active_game_id)})
                    return

                if self.path == "/api/chat":
                    if not _active_game_id:
                        raise ValueError("Start a game first")
                    message = str(payload.get("message", "")).strip()
                    if not message or len(message) > 4_000:
                        raise ValueError("Message must be between 1 and 4000 characters")
                    state = _full_state(_active_game_id)
                    lesson = _validate_demo_line(_active_game_id, _model_reply(message, state), state)
                    _chat_history.extend([
                        {"role": "user", "content": message},
                        {"role": "assistant", "content": lesson["message"]},
                    ])
                    self._serve_payload({"reply": lesson["message"], "lesson": lesson, "state": state})
                    return

                self.send_error(404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._serve_payload({"error": str(exc)}, status=400)
        except Exception as exc:  # Keep the browser-facing API JSON-shaped.
            self._serve_payload({"error": f"Dashboard error: {exc}"}, status=500)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(404, f"File not found: {path.name}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self, path: Path) -> None:
        if not path.exists():
            payload = json.dumps(None).encode("utf-8")
        else:
            payload = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _serve_payload(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def log_message(self, format: str, *args: object) -> None:
        # Silence per-request logs; only show startup message
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Chess Rocket Dashboard Server")
    parser.add_argument("--port", type=int, default=8088, help="Port (default: 8088)")
    args = parser.parse_args()

    server = http.server.HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Chess Rocket Dashboard: http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
