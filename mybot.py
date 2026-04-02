#!/usr/bin/env python3
"""
CopperHead Bot - 7crypt (Tournament Edition)

Strategy overview:
  1. FLOOD FILL  — simulate each candidate move and count reachable space.
                   Heavily penalise moves that trap us in a small region.
  2. BFS FOOD    — use breadth-first search for real shortest-path distance
                   instead of inaccurate Manhattan distance.
  3. HEAD DANGER — cells adjacent to the opponent's head are lethal if they
                   are >= our length.  If we are longer, treat them as kill
                   opportunities instead.
  4. VORONOI     — score each candidate by how many board cells we own vs
                   the opponent (using straight-line distance as a proxy so
                   it stays fast enough to run every tick).
  5. TAIL FOLLOW — when all else is equal, following our own tail is always
                   safe because the tail moves away in the next tick.
"""

import asyncio
import json
import argparse
import random
from collections import deque
import websockets


# ============================================================================
#  BOT CONFIGURATION
# ============================================================================

GAME_SERVER = "ws://localhost:8765/ws/"
BOT_NAME    = "7crypt"
BOT_VERSION = "2.0-tournament"


# ============================================================================
#  BOT CLASS
# ============================================================================

class MyBot:
    def __init__(self, server_url: str, name: str = None):
        self.server_url  = server_url
        self.name        = name or BOT_NAME
        self.player_id   = None
        self.game_state  = None
        self.running     = False
        self.room_id     = None
        self.grid_width  = 30
        self.grid_height = 20

    def log(self, msg: str):
        print(msg.encode("ascii", errors="replace").decode("ascii"))

    # ── connection helpers (unchanged) ────────────────────────────────────────

    async def wait_for_open_competition(self):
        import aiohttp
        base_url = self.server_url.rstrip("/")
        if base_url.endswith("/ws"):
            base_url = base_url[:-3]
        http_url = base_url.replace("ws://", "http://").replace("wss://", "https://")
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{http_url}/status") as resp:
                        if resp.status == 200:
                            self.log("Server reachable – joining lobby...")
                            return True
                        else:
                            self.log(f"Server not ready (status {resp.status}), waiting...")
            except Exception as e:
                self.log(f"Cannot reach server: {e}, retrying...")
            await asyncio.sleep(5)

    async def connect(self):
        await self.wait_for_open_competition()
        base_url = self.server_url.rstrip("/")
        if base_url.endswith("/ws"):
            base_url = base_url[:-3]
        url = f"{base_url}/ws/join"
        try:
            self.log(f"Connecting to {url}...")
            self.ws = await websockets.connect(url)
            self.log("Connected!  Joining lobby...")
            await self.ws.send(json.dumps({"action": "join", "name": self.name}))
            return True
        except Exception as e:
            self.log(f"Connection failed: {e}")
            return False

    async def play(self):
        if not await self.connect():
            self.log("Failed to connect.  Exiting.")
            return
        self.running = True
        try:
            while self.running:
                message = await self.ws.recv()
                data    = json.loads(message)
                await self.handle_message(data)
        except websockets.ConnectionClosed:
            self.log("Disconnected from server.")
        except Exception as e:
            self.log(f"Error: {e}")
        finally:
            self.running = False
            try:
                await self.ws.close()
            except Exception:
                pass
            self.log("Bot stopped.")

    async def handle_message(self, data: dict):
        msg_type = data.get("type")

        if msg_type == "error":
            self.log(f"Server error: {data.get('message', 'Unknown error')}")
            self.running = False

        elif msg_type == "joined":
            self.player_id = data.get("player_id")
            self.room_id   = data.get("room_id")
            self.log(f"Joined Arena {self.room_id} as Player {self.player_id}")
            await self.ws.send(json.dumps({"action": "ready", "mode": "two_player", "name": self.name}))
            self.log(f"Ready!  Playing as '{self.name}'")

        elif msg_type == "state":
            self.game_state = data.get("game")
            grid = self.game_state.get("grid", {})
            if grid:
                self.grid_width  = grid.get("width",  self.grid_width)
                self.grid_height = grid.get("height", self.grid_height)
            if self.game_state and self.game_state.get("running"):
                direction = self.calculate_move()
                if direction:
                    await self.ws.send(json.dumps({"action": "move", "direction": direction}))

        elif msg_type == "start":
            self.log("Game started!")

        elif msg_type == "gameover":
            winner    = data.get("winner")
            my_wins   = data.get("wins", {}).get(str(self.player_id), 0)
            opp_id    = 3 - self.player_id
            opp_wins  = data.get("wins", {}).get(str(opp_id), 0)
            pts       = data.get("points_to_win", 5)
            if winner == self.player_id:
                self.log(f"Won!  (Score: {my_wins}-{opp_wins}, first to {pts})")
            elif winner:
                self.log(f"Lost! (Score: {my_wins}-{opp_wins}, first to {pts})")
            else:
                self.log(f"Draw! (Score: {my_wins}-{opp_wins}, first to {pts})")
            await self.ws.send(json.dumps({"action": "ready", "name": self.name}))

        elif msg_type == "match_complete":
            winner_id   = data.get("winner", {}).get("player_id")
            winner_name = data.get("winner", {}).get("name", "Unknown")
            final_score = data.get("final_score", {})
            my_score    = final_score.get(str(self.player_id), 0)
            opp_id      = 3 - self.player_id
            opp_score   = final_score.get(str(opp_id), 0)
            if winner_id == self.player_id:
                self.log(f"Match WON!  Final: {my_score}-{opp_score}")
                self.log("Waiting for next round...")
            else:
                self.log(f"Match lost to {winner_name}.  Final: {my_score}-{opp_score}")
                self.log("Eliminated.  Exiting.")
                self.running = False

        elif msg_type == "match_assigned":
            self.room_id    = data.get("room_id")
            self.player_id  = data.get("player_id")
            self.game_state = None
            opponent = data.get("opponent", "Opponent")
            self.log(f"Next round!  Arena {self.room_id} vs {opponent}")
            await self.ws.send(json.dumps({"action": "ready", "name": self.name}))

        elif msg_type in ("lobby_joined", "lobby_update"):
            if msg_type == "lobby_joined":
                self.log(f"Joined lobby as '{data.get('name', self.name)}'")

        elif msg_type in ("lobby_left", "lobby_kicked"):
            self.log("Removed from lobby.")
            self.running = False

        elif msg_type == "competition_complete":
            champion = data.get("champion", {}).get("name", "Unknown")
            self.log(f"Tournament complete!  Champion: {champion}")
            self.running = False

        elif msg_type == "waiting":
            self.log("Waiting for opponent...")

    # ========================================================================
    #  AI — CALCULATE MOVE
    # ========================================================================

    def calculate_move(self) -> str | None:
        """
        Multi-layer evaluation (highest to lowest priority):

          Layer 0 — eliminate immediately fatal moves (wall / own-body collision)
          Layer 1 — flood fill:  prefer moves that keep the most space open
          Layer 2 — head safety: avoid cells the opponent can eat us from
          Layer 3 — food (BFS): take the shortest real path to the nearest food
          Layer 4 — Voronoi:    prefer moves that give us more board territory
          Layer 5 — tail bias:  tiebreak toward our own tail (always safe)
          Layer 6 — centre:     slight bias toward the middle of the board
        """
        if not self.game_state:
            return None

        snakes    = self.game_state.get("snakes", {})
        my_snake  = snakes.get(str(self.player_id))
        if not my_snake or not my_snake.get("body"):
            return None

        head        = my_snake["body"][0]           # [x, y]
        my_body     = my_snake["body"]
        my_length   = len(my_body)
        current_dir = my_snake.get("direction", "right")

        opp_id      = str(3 - self.player_id)
        opp_snake   = snakes.get(opp_id)
        opp_body    = opp_snake["body"] if opp_snake and opp_snake.get("body") else []
        opp_head    = opp_body[0] if opp_body else None
        opp_length  = len(opp_body)

        foods       = self.game_state.get("foods", [])
        food_set    = {(f["x"], f["y"]) for f in foods}

        W, H = self.grid_width, self.grid_height

        # ── direction maps ────────────────────────────────────────────────────
        DIRS = {"up": (0,-1), "down": (0,1), "left": (-1,0), "right": (1,0)}
        OPP  = {"up": "down", "down": "up", "left": "right", "right": "left"}

        # ── build occupied set ────────────────────────────────────────────────
        # We skip the LAST segment of every snake because it moves away this tick.
        # Exception: if a snake just ate food its tail does NOT shrink — but we
        # have no way to know that from the state, so being conservative (treating
        # tail as free) is fine; it's a minor inaccuracy.
        occupied = set()
        for snake_data in snakes.values():
            body = snake_data.get("body", [])
            for seg in body[:-1]:               # exclude tail
                occupied.add((seg[0], seg[1]))

        my_tail = (my_body[-1][0], my_body[-1][1])  # guaranteed safe next tick

        # ── helpers ───────────────────────────────────────────────────────────

        def in_bounds(x, y):
            return 0 <= x < W and 0 <= y < H

        def is_safe_cell(x, y):
            return in_bounds(x, y) and (x, y) not in occupied

        def flood_fill(start_x, start_y, blocked: set) -> int:
            """Count cells reachable from (start_x, start_y) avoiding `blocked`."""
            if not in_bounds(start_x, start_y):
                return 0
            visited = {(start_x, start_y)}
            q = deque([(start_x, start_y)])
            while q:
                cx, cy = q.popleft()
                for dx, dy in DIRS.values():
                    nx, ny = cx+dx, cy+dy
                    if in_bounds(nx, ny) and (nx, ny) not in blocked and (nx, ny) not in visited:
                        visited.add((nx, ny))
                        q.append((nx, ny))
            return len(visited)

        def bfs_distance(sx, sy, targets: set, blocked: set):
            """
            BFS from (sx,sy); return (distance, target_cell) to the nearest
            cell in `targets`, or (inf, None) if unreachable.
            """
            if not targets:
                return float('inf'), None
            visited = {(sx, sy)}
            q = deque([(sx, sy, 0)])
            while q:
                cx, cy, dist = q.popleft()
                if (cx, cy) in targets:
                    return dist, (cx, cy)
                for dx, dy in DIRS.values():
                    nx, ny = cx+dx, cy+dy
                    if in_bounds(nx, ny) and (nx, ny) not in blocked and (nx, ny) not in visited:
                        visited.add((nx, ny))
                        q.append((nx, ny, dist+1))
            return float('inf'), None

        # ── cells adjacent to opponent head (danger / opportunity) ────────────
        opp_reach = set()
        if opp_head:
            for dx, dy in DIRS.values():
                nx, ny = opp_head[0]+dx, opp_head[1]+dy
                if in_bounds(nx, ny):
                    opp_reach.add((nx, ny))

        # ── candidate moves ───────────────────────────────────────────────────
        candidates = []
        for direction, (dx, dy) in DIRS.items():
            if direction == OPP.get(current_dir):
                continue                         # can't reverse
            nx, ny = head[0]+dx, head[1]+dy
            if is_safe_cell(nx, ny):
                candidates.append((direction, nx, ny))

        # If ALL moves are blocked, at least don't reverse (instant death)
        if not candidates:
            for direction in DIRS:
                if direction != OPP.get(current_dir):
                    return direction
            return current_dir

        # ── score each candidate ──────────────────────────────────────────────
        scored = []

        for direction, nx, ny in candidates:
            score = 0

            # ── simulate the board after this move ────────────────────────────
            # Our head moves to (nx,ny); our tail frees up (already excluded).
            # For flood fill accuracy we add the new head position as occupied.
            sim_blocked = occupied | {(nx, ny)}
            # my_tail is already excluded from `occupied`; leave it free.

            # ────────────────────────────────────────────────────────────────
            # LAYER 1 — FLOOD FILL  (most important: survival)
            # ────────────────────────────────────────────────────────────────
            space = flood_fill(nx, ny, sim_blocked)

            if space == 0:
                score -= 1_000_000              # instant trap — avoid at all costs
            elif space < my_length // 2:
                score -= 80_000                 # very tight — strong penalty
            elif space < my_length:
                score -= 20_000                 # tight — penalty
            else:
                # Reward having lots of room; use log-like scaling
                score += min(space, W * H // 2) * 30

            # ────────────────────────────────────────────────────────────────
            # LAYER 2 — HEAD-ON COLLISION SAFETY / AGGRESSION
            # ────────────────────────────────────────────────────────────────
            if (nx, ny) in opp_reach:
                if opp_length >= my_length:
                    # Opponent can eat us here — heavily penalise
                    score -= 5_000
                else:
                    # We are longer — this is a kill opportunity!
                    score += 3_000

            # ────────────────────────────────────────────────────────────────
            # LAYER 3 — FOOD (BFS real distance)
            # ────────────────────────────────────────────────────────────────
            # Land directly on food
            if (nx, ny) in food_set:
                score += 4_000

            # BFS distance to nearest food
            if food_set and space >= my_length // 2:   # only chase food if safe
                food_dist, _ = bfs_distance(nx, ny, food_set, sim_blocked)
                if food_dist < float('inf'):
                    # Closer = better; scale inversely with distance
                    score += max(0, 1_000 - food_dist * 40)

                    # Bonus: if we reach food before opponent does
                    if opp_head:
                        opp_food_dist, _ = bfs_distance(
                            opp_head[0], opp_head[1], food_set, occupied)
                        if food_dist < opp_food_dist:
                            score += 600            # we win the food race

            # ────────────────────────────────────────────────────────────────
            # LAYER 4 — VORONOI TERRITORY CONTROL
            # (uses straight-line distance as a fast approximation)
            # ────────────────────────────────────────────────────────────────
            if opp_head:
                my_territory = 0
                for x in range(W):
                    for y in range(H):
                        if (x, y) not in sim_blocked:
                            my_dist  = abs(x - nx)         + abs(y - ny)
                            their_d  = abs(x - opp_head[0]) + abs(y - opp_head[1])
                            if my_dist < their_d:
                                my_territory += 1
                            elif their_d < my_dist:
                                my_territory -= 1
                score += my_territory * 4

            # ────────────────────────────────────────────────────────────────
            # LAYER 5 — TAIL BIAS  (safe fallback when space is tight)
            # ────────────────────────────────────────────────────────────────
            tail_dist = abs(nx - my_tail[0]) + abs(ny - my_tail[1])
            if space < my_length:
                # When cramped, actively chase our tail — it keeps us alive
                score += max(0, 300 - tail_dist * 20)

            # ────────────────────────────────────────────────────────────────
            # LAYER 6 — CENTRE PREFERENCE  (small tiebreaker)
            # ────────────────────────────────────────────────────────────────
            cx = abs(nx - W // 2)
            cy = abs(ny - H // 2)
            score -= (cx + cy) * 2              # mild pull toward centre

            scored.append((score, direction))

        # Return the highest-scoring direction
        scored.sort(key=lambda t: t[0], reverse=True)
        return scored[0][1]


# ============================================================================
#  MAIN
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(description="CopperHead Bot — 7crypt")
    parser.add_argument("--server", "-s", default=GAME_SERVER,
                        help=f"Server WebSocket URL (default: {GAME_SERVER})")
    parser.add_argument("--name", "-n", default=None,
                        help=f"Bot display name (default: {BOT_NAME})")
    parser.add_argument("--difficulty", "-d", type=int, default=10,
                        help="Accepted for compatibility (not used)")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="Suppress console output")
    args = parser.parse_args()

    bot = MyBot(args.server, name=args.name)
    print(f"{bot.name} v{BOT_VERSION}")
    print(f"  Server: {args.server}")
    print()
    await bot.play()


if __name__ == "__main__":
    asyncio.run(main())