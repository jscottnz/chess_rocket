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
import random
import sys
import subprocess
import threading
import tempfile
from pathlib import Path

import chess

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"
_NOTES_FILE = _DATA_DIR / "notes.json"
_DASHBOARD_HTML = Path(__file__).resolve().parent / "dashboard.html"
_PUZZLES_DIR = _PROJECT_ROOT / "puzzles"

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
_active_puzzle: dict | None = None
_puzzle_board: chess.Board | None = None
_puzzle_move_index = 0
_puzzle_snapshots: list[tuple[chess.Board, int]] = []
_state_lock = threading.RLock()
_chat_history: list[dict[str, str]] = []


def _log(message: str) -> None:
    """Write dashboard diagnostics to stdout immediately."""
    print(f"[dashboard] {message}", flush=True)


def _load_notes() -> str:
    """Load player notes shared across all dashboard games."""
    try:
        if _NOTES_FILE.exists():
            payload = json.loads(_NOTES_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return str(payload.get("notes", ""))[:20_000]
    except (OSError, json.JSONDecodeError):
        _log("Could not read persistent notes")
    return ""


def _save_notes(notes: str) -> str:
    """Persist player notes atomically and return the saved text."""
    notes = str(notes)[:20_000]
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = _NOTES_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"notes": notes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, _NOTES_FILE)
    return notes

_COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "demo_title": {"type": "string"},
        "demo_moves": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "annotations": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["square", "piece"]},
                    "square": {"type": "string"},
                    "piece": {"type": "string", "enum": ["k", "q", "r", "b", "n", "p", ""]},
                    "color": {"type": "string", "enum": ["white", "black", ""]},
                },
                "required": ["text", "kind", "square", "piece", "color"],
                "additionalProperties": False,
            },
        },
        "jargon": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "term": {"type": "string"},
                },
                "required": ["text", "term"],
                "additionalProperties": False,
            },
        },
        "notes_update": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["none", "replace"]},
                "text": {"type": "string"},
            },
            "required": ["action", "text"],
            "additionalProperties": False,
        },
    },
    "required": ["message", "demo_title", "demo_moves", "annotations", "jargon", "notes_update"],
    "additionalProperties": False,
}


def _full_state(game_id: str) -> dict:
    """Return the dashboard's full state contract, not the minified MCP result."""
    game = _game_service._games.get(game_id)
    if game is None:
        return {"error": f"Game not found: {game_id}"}
    return _game_service._build_game_state(game_id, game)


def _load_puzzle_candidates() -> list[dict]:
    candidates = []
    for path in sorted(_PUZZLES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for puzzle in payload:
            if isinstance(puzzle, dict) and puzzle.get("fen") and puzzle.get("solution_moves"):
                puzzle = dict(puzzle)
                puzzle["_source_file"] = path.name
                candidates.append(puzzle)
    return candidates


def _puzzle_state() -> dict:
    if _active_puzzle is None or _puzzle_board is None:
        return {"error": "No active puzzle"}
    board = _puzzle_board
    move_list = []
    replay = chess.Board(_active_puzzle["fen"])
    for move in board.move_stack:
        move_list.append(replay.san(move))
        replay.push(move)
    last_move = board.peek().uci() if board.move_stack else None
    state = {
        "game_id": "puzzle",
        "puzzle_mode": True,
        "fen": board.fen(),
        "board_display": str(board),
        "move_list": move_list,
        "last_move": last_move,
        "last_move_san": move_list[-1] if move_list else None,
        "eval_score": None,
        "player_color": "white" if chess.Board(_active_puzzle["fen"]).turn == chess.WHITE else "black",
        "target_elo": _active_puzzle.get("difficulty_rating", 1000),
        "is_game_over": False,
        "result": None,
        "legal_moves": [board.san(move) for move in board.legal_moves],
        "accuracy": {"white": 0.0, "black": 0.0},
        "session_number": 1,
        "streak": 0,
        "lesson_name": "Puzzle",
        "current_opening": None,
        "material": _game_service._count_material(board),
        "captured_pieces": _game_service._get_captured_pieces(board),
        "is_check": board.is_check(),
        "is_checkmate": board.is_checkmate(),
        "is_stalemate": board.is_stalemate(),
        "move_annotations": [],
        "puzzle": {
            "motif": _active_puzzle.get("motif", "tactics"),
            "difficulty": _active_puzzle.get("difficulty", "intermediate"),
            "source": _active_puzzle.get("_source_file", "puzzles"),
            "explanation": _active_puzzle.get("explanation", "Find the strongest move."),
            "status": "solved" if _puzzle_move_index >= len(_active_puzzle["solution_moves"]) else "in_progress",
        },
    }
    return state


def _start_puzzle(difficulty: str = "all", motif: str = "all") -> dict:
    global _active_puzzle, _puzzle_board, _puzzle_move_index, _puzzle_snapshots, _active_game_id
    candidates = _load_puzzle_candidates()
    filtered = [
        puzzle for puzzle in candidates
        if (difficulty == "all" or puzzle.get("difficulty") == difficulty)
        and (motif == "all" or puzzle.get("motif") == motif)
    ]
    if not filtered:
        raise ValueError("No puzzles match those filters")
    _active_game_id = None
    _active_puzzle = random.choice(filtered)
    _puzzle_board = chess.Board(_active_puzzle["fen"])
    _puzzle_move_index = 0
    _puzzle_snapshots = [(_puzzle_board.copy(), 0)]
    return _puzzle_state()


def _capture_annotation(before: chess.Board, result: dict) -> dict | None:
    """Describe a black capture made by the move in a compact UI-friendly form."""
    if before.turn != chess.BLACK:
        return None
    uci = str(result.get("last_move", ""))
    if len(uci) < 4:
        return None
    move = chess.Move.from_uci(uci[:5])
    captured = before.piece_at(move.to_square)
    if captured is None and before.is_en_passant(move):
        captured = before.piece_at(move.to_square - 8)
    if captured is None:
        return None
    return {
        "captured_piece": captured.symbol().lower(),
        "captured_color": "white" if captured.color else "black",
        "square": chess.square_name(move.to_square),
        "move": str(result.get("last_move_san", "")),
    }


def _engine_analysis(game_id: str, board: chess.Board | None = None) -> list[dict]:
    """Return fresh multi-PV Stockfish analysis for the tutor prompt."""
    game = _game_service._games.get(game_id)
    if game is None:
        return []
    try:
        analysis_board = board.copy() if board is not None else game["board"].copy()
        if analysis_board.is_game_over():
            return []
        depth = max(12, min(22, int(os.environ.get("COACH_ENGINE_DEPTH", "18"))))
        return game["engine"].analyze_position(analysis_board, depth=depth, multipv=3)
    except (OSError, ValueError, RuntimeError, chess.engine.EngineError) as exc:
        _log(f"Coach engine analysis unavailable: {exc}")
        return []


def _puzzle_move(uci: str) -> dict:
    global _puzzle_board, _puzzle_move_index, _puzzle_snapshots
    if _active_puzzle is None or _puzzle_board is None:
        raise ValueError("Start a puzzle first")
    if _puzzle_move_index >= len(_active_puzzle["solution_moves"]):
        raise ValueError("Puzzle already solved")
    move = chess.Move.from_uci(uci)
    if move not in _puzzle_board.legal_moves:
        raise ValueError("That move is not legal in this position")
    expected = chess.Move.from_uci(_active_puzzle["solution_moves"][_puzzle_move_index])
    if move != expected:
        return {
            "state": _puzzle_state(),
            "reply": "That move does not solve the puzzle. Look for the tactical idea and try again.",
            "correct": False,
        }
    _puzzle_snapshots.append((_puzzle_board.copy(), _puzzle_move_index))
    san = _puzzle_board.san(move)
    _puzzle_board.push(move)
    _puzzle_move_index += 1
    reply = "Correct. Find the next move in the sequence."
    if _puzzle_move_index < len(_active_puzzle["solution_moves"]):
        response = chess.Move.from_uci(_active_puzzle["solution_moves"][_puzzle_move_index])
        if response in _puzzle_board.legal_moves:
            response_san = _puzzle_board.san(response)
            _puzzle_board.push(response)
            _puzzle_move_index += 1
            reply = f"Correct: {san}. The position replies with {response_san}. Continue the combination."
    if _puzzle_move_index >= len(_active_puzzle["solution_moves"]):
        reply = f"Puzzle solved: {san}. {_active_puzzle.get('explanation', '')}"
    return {"state": _puzzle_state(), "reply": reply, "correct": True}


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
        position["engine_analysis"] = move_context.get("engine_analysis", [])
        position["coach_move_analysis"] = move_context.get("pre_engine_analysis", [])

    history = "\n".join(
        f"{item['role'].title()}: {item['content']}" for item in _chat_history[-10:]
    )
    notes = _load_notes()
    prompt = (
        "You are the Chess Rocket tutor. Reply directly to the player in a warm, "
        "concise, encouraging way. Adjust vocabulary to their Elo. Never shame the "
        "player and do not call a move a 'blunder', 'serious blunder', 'mistake', "
        "'bad move', or similar label in your prose. Instead describe the chess "
        "effect: say what the move weakened, which piece or square became vulnerable, "
        "what threat was missed, and what practical repair or habit would help. For "
        "example: 'That move weakened your position because it left the rook loose; "
        "let's look at how to protect it.' Treat classification and centipawn loss as "
        "private coaching signals, not language to repeat to the player. Explain one actionable idea "
        "and use the private position analysis when supplied. The position and chess state "
        "below are authoritative. "
        "After the player makes a move, do not volunteer the next best move or tell them "
        "what to play. Explain the consequences of their move and what to inspect next. "
        "Only give a concrete move recommendation or variation when the player explicitly "
        "asks what to play, asks for the best move, requests a recommendation, or asks "
        "to see a line. "
        "Do not call tools, do not change files, and do not make an unverified tactical "
        "claim. Private analysis is included below. "
        "If you recommend a concrete move, it must be one of the top candidates or a "
        "directly calculated continuation from one of them. "
        "Never call a move 'best' unless the analysis supports it; if your earlier "
        "message conflicts with the analysis, explicitly acknowledge and correct it. "
        "Treat the analysis as private: never mention Stockfish, the engine, engine "
        "depth, centipawns, or that you consulted an engine. Explain the resulting "
        "chess idea, trade-offs, threats, and consequences as your own coaching. "
        "When the player asks for a recommendation, first use the private analysis "
        "to select the sound candidate, then explain why it works and what the player "
        "should watch for afterward. "
        "not claim to have changed the board. If a concrete variation or idea is "
        "helpful, include up to 8 legal SAN moves starting from the exact current "
        "position in demo_moves; otherwise use an empty array. Also return explicit "
        "annotations for board references in your message. Each annotation's text "
        "must be an exact phrase from message. For a location use kind square and "
        "its square. For a piece phrase such as 'your bishop', use kind piece and "
        "the exact square of that specific piece; never annotate every piece of a "
        "type. If you cannot identify the exact piece, omit the annotation. Return "
        "jargon annotations for specialized chess terms in your message. Each jargon "
        "annotation must have text as an exact phrase from message and term as the "
        "short term to explain. Do not annotate ordinary words like move, piece, or "
        "good. Return "
        "the requested JSON object only. Your response must also include notes_update. "
        "Use action none and an empty text when no note change is needed. Use action "
        "replace with the complete corrected notes when the player asks to update notes "
        "or when you identify a clear factual chess correction in them. Preserve useful "
        "existing notes when replacing them; do not rewrite notes unnecessarily.\n\n"
        f"Position: {json.dumps(position, ensure_ascii=False)}\n"
        f"Persistent player notes (shared across games):\n{notes or '(none)'}\n"
        f"Private position analysis (authoritative for concrete recommendations; do not mention its source):\n"
        f"{json.dumps(position.get('engine_analysis', []), ensure_ascii=False)}\n"
        f"Conversation:\n{history or '(none)'}\n"
        f"Player request: {user_message}"
    )
    if move_context and move_context.get("explain_engine_move") and move_context.get("engine_move"):
        prompt += (
            "\n\nThe player enabled engine-move explanations. Explain why the coach's "
            f"move {move_context['engine_move']} was played, focusing on the plan, "
            "threat, or positional reason rather than only naming the move. Do not "
            "mention how the move was selected. If this is Black's move, explicitly "
            "refer to Black."
        )
    with tempfile.TemporaryDirectory(prefix="chess-rocket-codex-") as temp_dir:
        schema_path = Path(temp_dir) / "coach-schema.json"
        answer_path = Path(temp_dir) / "coach-answer.json"
        schema_path.write_text(json.dumps(_COACH_SCHEMA), encoding="utf-8")
        command = [
            os.environ.get("CODEX_BIN", "codex"),
            "-a", "never",
            "-m", os.environ.get("CODEX_MODEL", "gpt-5.6-luna"),
            "-c", f"model_reasoning_effort={os.environ.get('CODEX_REASONING_EFFORT', 'medium')}",
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
        _log(f"starting Codex model={os.environ.get('CODEX_MODEL', 'gpt-5.6-luna')} reasoning={os.environ.get('CODEX_REASONING_EFFORT', 'medium')}")
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
            _log(f"Codex exited status={result.returncode}")
            if result.stderr.strip():
                _log(f"Codex stderr: {result.stderr.strip()[-1000:]}")
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
                            lesson = {"message": item.get("text", ""), "demo_title": "", "demo_moves": [], "annotations": []}
                        break
            if result.returncode != 0 and not lesson:
                detail = result.stderr.strip() or f"codex exited with status {result.returncode}"
                _log(f"Codex request failed: {detail}")
                lesson = {"message": f"Local Codex is unavailable: {detail}", "demo_title": "", "demo_moves": [], "annotations": [], "jargon": [], "notes_update": {"action": "none", "text": ""}}
            if not isinstance(lesson, dict):
                lesson = {"message": "I couldn't produce a coaching note.", "demo_title": "", "demo_moves": [], "annotations": [], "jargon": [], "notes_update": {"action": "none", "text": ""}}
            notes_update = lesson.get("notes_update", {})
            if isinstance(notes_update, dict) and notes_update.get("action") == "replace":
                _save_notes(str(notes_update.get("text", "")))
            jargon = []
            for item in lesson.get("jargon", []) if isinstance(lesson.get("jargon", []), list) else []:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                term = str(item.get("term", "")).strip()
                if text and term and re.search(re.escape(text), str(lesson.get("message", "")), re.IGNORECASE):
                    jargon.append({"text": text, "term": term[:120]})
            return {
                "message": str(lesson.get("message", "I couldn't produce a coaching note.")),
                "demo_title": str(lesson.get("demo_title", "")),
                "demo_moves": [str(move) for move in lesson.get("demo_moves", [])][:8],
                "annotations": lesson.get("annotations", []) if isinstance(lesson.get("annotations", []), list) else [],
                "jargon": jargon[:12],
                "notes": _load_notes(),
            }
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            _log(f"Codex request exception: {exc}")
            return {"message": f"Local Codex is unavailable: {exc}", "demo_title": "", "demo_moves": [], "annotations": [], "jargon": [], "notes": _load_notes()}


def _validate_demo_line(
    game_id: str,
    lesson: dict,
    state: dict,
    engine_analysis: list[dict] | None = None,
) -> dict:
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
    if validated and engine_analysis:
        engine_first_moves = {
            line.get("pv", [])[0]
            for line in engine_analysis
            if isinstance(line, dict) and line.get("pv")
        }
        if engine_first_moves and validated[0]["san"] not in engine_first_moves:
            _log(
                f"Discarding unverified teaching line starting with {validated[0]['san']}"
            )
            validated = []
    lesson["demo_moves"] = validated
    _validate_annotations(game_id, lesson, state)
    return lesson


def _validate_annotations(game_id: str, lesson: dict, state: dict) -> None:
    """Keep only annotations that refer to an exact current board target."""
    game = _game_service._games.get(game_id)
    board = game["board"] if game is not None else None
    message = lesson.get("message", "")
    annotations = lesson.get("annotations", [])
    valid = []
    if board is None or not isinstance(annotations, list):
        lesson["annotations"] = []
        return
    for annotation in annotations[:12]:
        if not isinstance(annotation, dict):
            continue
        text = str(annotation.get("text", "")).strip()
        kind = annotation.get("kind")
        square = str(annotation.get("square", "")).lower()
        piece = str(annotation.get("piece", "")).lower()
        color = str(annotation.get("color", "")).lower()
        if not text or not re.search(re.escape(text), message, re.IGNORECASE):
            continue
        if kind not in ("square", "piece") or not re.fullmatch(r"[a-h][1-8]", square):
            continue
        board_piece = board.piece_at(_game_service.chess.parse_square(square))
        if kind == "piece":
            if board_piece is None or piece != board_piece.symbol().lower() or color != ("white" if board_piece.color else "black"):
                continue
        valid.append({"text": text, "kind": kind, "square": square, "piece": piece, "color": color})
    lesson["annotations"] = valid


def _requests_demo_line(message: str) -> bool:
    """Return whether the player explicitly asked to see moves or a variation."""
    return bool(re.search(
        r"\b(show|demonstrate|variation|line|moves?|sequence|play out|what should I play|how should I play)\b",
        message,
        re.IGNORECASE,
    ))


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
            elif _active_puzzle is not None:
                with _state_lock:
                    self._serve_payload(_puzzle_state())
            else:
                # Do not display a stale game written by the separate MCP
                # console process. The dashboard owns its own game session.
                self._serve_payload(None)
        elif self.path == "/api/progress":
            self._serve_json(_DATA_DIR / "progress.json")
        elif self.path == "/api/notes":
            self._serve_payload({"notes": _load_notes()})
        elif self.path == "/api/chat/history":
            self._serve_payload({"messages": _chat_history})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        global _active_game_id, _active_puzzle, _puzzle_board, _puzzle_move_index, _puzzle_snapshots
        try:
            payload = _json_body(self)
            with _state_lock:
                if self.path == "/api/notes":
                    notes = _save_notes(str(payload.get("notes", "")))
                    self._serve_payload({"notes": notes})
                    return
                if self.path == "/api/game/new":
                    target_elo = int(payload.get("target_elo", 800))
                    player_color = payload.get("player_color", "white")
                    if player_color not in ("white", "black"):
                        raise ValueError("player_color must be white or black")
                    _active_puzzle = None
                    _puzzle_board = None
                    _puzzle_move_index = 0
                    _puzzle_snapshots = []
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
                    player_board_before = board.copy()
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
                    player_state = _game_service.make_move(_active_game_id, san)
                    capture = _capture_annotation(player_board_before, player_state)
                    state = _full_state(_active_game_id)
                    engine_move_san = ""
                    pre_engine_analysis = []
                    if not state["is_game_over"]:
                        engine_board_before = game["board"].copy()
                        if bool(payload.get("explain_engine_move", False)):
                            pre_engine_analysis = _engine_analysis(_active_game_id, engine_board_before)
                        engine_state = _game_service.engine_move(_active_game_id)
                        engine_move_san = str(engine_state.get("last_move_san", ""))
                        capture = _capture_annotation(engine_board_before, engine_state) or capture
                        state = _full_state(_active_game_id)
                    state["black_capture"] = capture
                    engine_analysis = _engine_analysis(_active_game_id)
                    coach_prompt = (
                        f"The player played {san}. Give a brief coaching explanation, "
                        "then mention what to look for in the next position."
                    )
                    lesson = _model_reply(coach_prompt, state, {
                        "move": san,
                        "classification": evaluation.get("classification"),
                        "cp_loss": evaluation.get("cp_loss"),
                        "best_move": evaluation.get("best_move_san"),
                        "engine_move": engine_move_san,
                        "explain_engine_move": bool(payload.get("explain_engine_move", False)),
                        "pre_engine_analysis": pre_engine_analysis,
                        "engine_analysis": engine_analysis,
                    })
                    if not bool(payload.get("show_hint", False)):
                        lesson["demo_moves"] = []
                    lesson = _validate_demo_line(_active_game_id, lesson, state, engine_analysis)
                    _chat_history.extend([
                        {"role": "user", "content": f"I played {san}."},
                        {"role": "assistant", "content": lesson["message"]},
                    ])
                    self._serve_payload({"state": state, "evaluation": evaluation, "reply": lesson["message"], "lesson": lesson})
                    return

                if self.path == "/api/puzzle/new":
                    state = _start_puzzle(
                        str(payload.get("difficulty", "all")),
                        str(payload.get("motif", "all")),
                    )
                    _chat_history.clear()
                    self._serve_payload({"state": state, "puzzle": state["puzzle"]})
                    return

                if self.path == "/api/puzzle/move":
                    result = _puzzle_move(str(payload.get("move", "")))
                    self._serve_payload(result, status=200)
                    return

                if self.path == "/api/puzzle/undo":
                    if _active_puzzle is None or _puzzle_board is None or len(_puzzle_snapshots) <= 1:
                        raise ValueError("There is no puzzle move to undo")
                    snapshot, move_index = _puzzle_snapshots.pop()
                    _puzzle_board = snapshot.copy()
                    _puzzle_move_index = move_index
                    self._serve_payload({"state": _puzzle_state()})
                    return

                if self.path == "/api/game/engine-move":
                    if not _active_game_id:
                        raise ValueError("Start a game first")
                    game = _game_service._games.get(_active_game_id)
                    if game is None or game["board"].is_game_over():
                        raise ValueError("No engine move is available")
                    board_before = game["board"].copy()
                    engine_state = _game_service.engine_move(_active_game_id)
                    state = _full_state(_active_game_id)
                    state["black_capture"] = _capture_annotation(board_before, engine_state)
                    self._serve_payload({"state": state})
                    return

                if self.path == "/api/game/undo":
                    if not _active_game_id:
                        raise ValueError("Start a game first")
                    result = _game_service.undo_move(_active_game_id)
                    if result.get("error"):
                        raise ValueError(result["error"])
                    # Remove the move and its automatic coaching response from the
                    # private conversation so future advice follows the new line.
                    move_history_index = next(
                        (
                            index for index in range(len(_chat_history) - 1, -1, -1)
                            if _chat_history[index].get("role") == "user"
                            and _chat_history[index].get("content", "").startswith("I played ")
                        ),
                        None,
                    )
                    if move_history_index is not None:
                        del _chat_history[move_history_index:]
                    state = _full_state(_active_game_id)
                    state["black_capture"] = None
                    self._serve_payload({"state": state})
                    return

                if self.path == "/api/chat":
                    if not _active_game_id and _active_puzzle is None:
                        raise ValueError("Start a game first")
                    message = str(payload.get("message", "")).strip()
                    if not message or len(message) > 4_000:
                        raise ValueError("Message must be between 1 and 4000 characters")
                    state = _full_state(_active_game_id) if _active_game_id else _puzzle_state()
                    engine_analysis = _engine_analysis(_active_game_id) if _active_game_id else []
                    lesson = _validate_demo_line(
                        _active_game_id,
                        _model_reply(message, state, {"engine_analysis": engine_analysis}),
                        state,
                        engine_analysis,
                    )
                    if not _requests_demo_line(message):
                        lesson["demo_moves"] = []
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

    def log_request(self, code: int | str, size: int | str = "-") -> None:
        """Suppress normal HTTP access logs."""
        pass

    def log_error(self, format: str, *args: object) -> None:
        """Keep HTTP errors visible without logging every successful request."""
        _log(f"HTTP error: {format % args}")

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler routes some diagnostics here; normal requests
        # are handled by log_request above and intentionally remain quiet.
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Chess Rocket Dashboard Server")
    parser.add_argument("--port", type=int, default=8088, help="Port (default: 8088)")
    args = parser.parse_args()

    server = http.server.HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    _log(f"Chess Rocket Dashboard: http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
