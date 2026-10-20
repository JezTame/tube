#!/usr/bin/env python3
"""
Tube 1.1 - A simple Underground-style map generator.

Usage:
    python tube.py myfirst.tube [my.coords] [--legend]

Tube will save coordinates in a simple text file that can be edited and
used for subsequent runs, allowing a simple map to be created, and then 
new connections to be added without moving the original stations.
A simple legend showing the names of lines can be added using --legend.

Tube input files have this format:
line Smith [color=red, direction=h]
    Alice -- Bob -- Charles -- Diana;

The direction (h, v, d or u) is a preference. For consistency and
compatibility with svgwrite, "color" has the US spelling. The default
color for a line is black.

Two stations may be connected by more than one line, but more than three
connections may not be as visually appealing.

Optional station-free rounded corners may be drawn with lines such as: 
bend Charles Edward [order=hv, color=red]
Order hv means horizontal first; vh means vertical first.
"""
import re
import sys
import math
import random
import shlex
from pathlib import Path

import svgwrite
import networkx as nx
from itertools import combinations

# Set SHOW_LABELS to False to block station labelling
SHOW_LABELS = True

# Values for the circle radii for interchange and normal stations
INTERCHANGE = 10
LOCAL = 6

SPACING = 90
MARGIN = 65
FONT_SIZE = 15
TEXT_HEIGHT = FONT_SIZE * 1.2
CHAR_WIDTH = FONT_SIZE * 0.60

DIRECTIONS = {
    "h": (1, 0),
    "v": (0, 1),
    "d": (1, 1),
    "u": (1, -1),
}

# Change the weights to produce a different map
WEIGHTS = {
    "station": 10000,
    "line": 10000,
    "crossing": 500,
    "geometry": 2,
    "diagonal": 80,
    "direction": 10,
    "length": 50,
    "bend": 50,
    "arc": 120,  # Encourage compact, equally sized bend legs.
    "arc_tangent": 150,  # Align route sections with the bend ends.
}

def cross(a, b):
    """2D cross product."""
    return a[0]*b[1] - a[1]*b[0]


def segment_intersection(a, b, c, d):
    """
    Find the intersection of segments AB and CD.

    Returns:
        None                 No intersection
        ("point", (x, y))    Intersection at one point
        ("overlap", None)    Collinear segments overlap
    """
    eps = 1e-9

    r = (b[0]-a[0], b[1]-a[1])
    s = (d[0]-c[0], d[1]-c[1])
    q = (c[0]-a[0], c[1]-a[1])

    denominator = cross(r, s)

    if abs(denominator) > eps:
        t = cross(q, s) / denominator
        u = cross(q, r) / denominator

        if -eps <= t <= 1+eps and -eps <= u <= 1+eps:
            p = (a[0]+t*r[0], a[1]+t*r[1])
            return ("point", p)

        return None

    # Parallel, but not on the same infinite line.
    if abs(cross(q, r)) > eps:
        return None

    # Collinear: find endpoints belonging to both segments.
    def on_segment(p, v, w):
        return (
            min(v[0], w[0])-eps <= p[0] <= max(v[0], w[0])+eps
            and min(v[1], w[1])-eps <= p[1] <= max(v[1], w[1])+eps
        )

    common = set()

    for p in (a, b, c, d):
        if on_segment(p, a, b) and on_segment(p, c, d):
            common.add(p)

    if len(common) >= 2:
        return ("overlap", None)

    if len(common) == 1:
        return ("point", common.pop())

    return None

def read_tube(filename):
    """Read line definitions from a .tube file."""
    #source = Path(filename).read_text(encoding="utf-8")
    rawlines = Path(filename).read_text(encoding="utf-8").splitlines()
    source_lines = [line for line in rawlines if not line.lstrip().startswith("#")]
    bends = []
    route_lines = []
    bend_re = re.compile(r"^\s*bend\s+(.+?)\s*\[([^\]]*)\]\s*;?\s*$")
    for text in source_lines:
        if text.lstrip().startswith("bend "):
            match = bend_re.fullmatch(text)
            if not match:
                raise ValueError(f"Invalid bend directive: {text}")
            names = shlex.split(match.group(1))
            if len(names) != 2:
                raise ValueError("Bend requires two station names; quote names with spaces")
            opts = {}
            for item in match.group(2).split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    opts[k.strip()] = v.strip()
            if opts.get("order", "hv") not in ("hv", "vh"):
                raise ValueError("Bend order must be hv or vh")
            if set(opts) - {"order", "color"}:
                raise ValueError(f"Unknown bend options: {set(opts) - {'order', 'color'}}")
            bends.append({"a": names[0], "b": names[1],
                          "order": opts.get("order", "hv"),
                          "color": opts.get("color")})
        else:
            route_lines.append(text)
    source = "\n".join(route_lines)

    pattern = re.compile(
        r"^\s*line\s+(\w+)\s*\[([^\]]*)\]\s*(.*?)"
        r"(?=^\s*line\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )

    lines = []
    for match in pattern.finditer(source):
        name, options, stations_text = match.groups()

        attributes = {}
        for item in options.split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                attributes[key.strip()] = value.strip()

        stations = [
            s.strip()
            for s in stations_text.strip().rstrip(";").split("--")
            if s.strip()
        ]

        if not stations:
            raise ValueError(f"Line {name} has no stations")

        direction = attributes.get("direction", "h")
        if direction not in DIRECTIONS:
            raise ValueError(f"Direction {direction} is not known.")

        lines.append({
            "name": name,
            "color": attributes.get("color", "black"),
            "direction": direction,
            "stations": stations,
        })

    if not lines:
        raise ValueError("No line definitions found")

    for bend in bends:
        a, b = bend["a"], bend["b"]
        if a == b:
            raise ValueError(f"Bend endpoints must be distinct: {a}, {b}")
        colors_a = {line["color"] for line in lines if a in line["stations"]}
        colors_b = {line["color"] for line in lines if b in line["stations"]}
        common = colors_a & colors_b
        if bend["color"]:
            pass  # Explicit bend colour overrides route colours.
        elif len(common) == 1:
            bend["color"] = next(iter(common))
        else:
            raise ValueError(f"Bend {a}--{b}: ambiguous/no common color; specify color=...")
    return lines, bends

def arrange(lines, fixed_positions=None, bends=()):
    """Place stations, joining lines at shared stations."""
    #positions = {}
    positions = dict(fixed_positions or {})

    for line in lines:
        stations = line["stations"]
        direction = line["direction"]

        # Find an already-positioned interchange.
        shared = [
            (i, s) for i, s in enumerate(stations)
            if s in positions
        ]

        if shared:
            index, station = shared[0]
            ax, ay = positions[station]
        else:
            # Start a new disconnected component.
            index = 0
            ax = (len(positions) + 1) * SPACING * 2 if positions else 0
            ay = 0

        for i, station in enumerate(stations):
            offset = (i - index) * SPACING

            dx, dy = DIRECTIONS[direction]
            x = ax + offset * dx
            y = ay + offset * dy

            # Existing interchanges retain their positions.
            if station not in positions:
                positions[station] = (x, y)

    # Introduce stations named only in bend directives.  Work through
    # the bends repeatedly, so a chain of new stations grows from any
    # endpoint already present on a route or in the coordinate file.
    pending = list(bends)
    while pending:
        remaining = []
        made_progress = False
        for bend in pending:
            a, b = bend["a"], bend["b"]
            if a in positions and b in positions:
                continue
            if a in positions or b in positions:
                known = a if a in positions else b
                missing = b if known == a else a
                x, y = positions[known]
                # Diagonal neighbours give the optimiser a compact,
                # non-degenerate starting geometry for an arc.
                delta = SPACING if known == a else -SPACING
                positions[missing] = (x + delta, y + delta)
                made_progress = True
            else:
                remaining.append(bend)
        if not remaining:
            break
        if not made_progress:
            # A disconnected group of bend-only stations has no anchor.
            # Seed one endpoint; the next pass places its neighbour.
            bend = remaining[0]
            x = (len(positions) + 1) * 2 * SPACING
            positions[bend["a"]] = (x, 0)
        pending = remaining

    return positions

def arc_path(a, b, order="hv"):
    """Return a rounded horizontal/vertical connection path in local SVG coordinates."""
    x1, y1 = a
    x2, y2 = b
    dx, dy = x2-x1, y2-y1
    if dx == 0 or dy == 0:
        return None  # Impossible to make a genuine 90-degree corner.
    r = min(abs(dx), abs(dy)) * 0.5
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    if order == "hv":
        entry = (x2-sx*r, y1)
        exit = (x2, y1+sy*r)
        sweep = int(sx == sy)
    else:
        entry = (x1, y2-sy*r)
        exit = (x1+sx*r, y2)
        sweep = int(sx != sy)
    return (f"M {x1},{y1} L {entry[0]},{entry[1]} "
            f"A {r},{r} 0 0 {sweep} {exit[0]},{exit[1]} "
            f"L {x2},{y2}")


def draw_map(lines, positions, filename, labels=None, legend=False, bends=()):
    """Render an SVG diagram."""

    if labels is None:
        labels = place_labels(lines, positions)

    min_x = min(x for x, y in positions.values()) - 12
    min_y = min(y for x, y in positions.values()) - 12
    max_x = max(x for x, y in positions.values()) + 12
    max_y = max(y for x, y in positions.values()) + 12

    # Include the bounding rectangles of all labels.
    for station, (tx, ty) in labels.items():

        text_width = len(station) * CHAR_WIDTH

        min_x = min(min_x, tx)
        max_x = max(max_x, tx + text_width)

        # SVG text coordinates specify the baseline.
        min_y = min(min_y, ty - TEXT_HEIGHT)
        max_y = max(max_y, ty + 3)

    # width = max_x - min_x + 2 * MARGIN + 110
    # height = max_y - min_y + 2 * MARGIN + 50
    width = max_x - min_x + 2 * MARGIN
    height = max_y - min_y + 2 * MARGIN

    # Reserve space below the diagram for a legend.
    if legend:
        map_height = height
        legend_gap = 30
        legend_row = 28

        legend_width = max(
            len(line["name"]) * CHAR_WIDTH for line in lines
        ) + 90

        width = max(width, legend_width + 2 * MARGIN)
        height += legend_gap + len(lines) * legend_row + 20

    dwg = svgwrite.Drawing(
        str(filename),
        size=(f"{width}px", f"{height}px"),
        viewBox=f"0 0 {width} {height}",
    )

    dwg.add(dwg.rect(
        insert=(0, 0),
        size=(width, height),
        fill="white",
    ))

    def point(station):
        x, y = positions[station]
        return (x - min_x + MARGIN,
                y - min_y + MARGIN)

    # Group route segments by their two end stations.  An undirected
    # key recognises the same track even when routes list it in
    # opposite directions.  Retain the first route's orientation so
    # its successive shared segments use a consistent colour order.
    tracks = {}
    for line in lines:
        for a, b in zip(line["stations"], line["stations"][1:]):
            key = frozenset((a, b))
            if key not in tracks:
                tracks[key] = {"ends": (a, b), "colors": []}
            if line["color"] not in tracks[key]["colors"]:
                tracks[key]["colors"].append(line["color"])

    # Single routes retain their original 8-pixel width. Shared routes
    # use touching 6-pixel bands (12 pixels for two colours).
    # For thicker lines in the output, try for example
    # SINGLE_WIDTH = 12
    # BAND_WIDTH = 8
    SINGLE_WIDTH = 8
    BAND_WIDTH = 6

    for track in tracks.values():
        a, b = track["ends"]
        colors = track["colors"]
        x1, y1 = point(a)
        x2, y2 = point(b)

        if len(colors) == 1:
            dwg.add(dwg.line(
                start=(x1, y1), end=(x2, y2),
                stroke=colors[0], stroke_width=SINGLE_WIDTH,
                stroke_linecap="round",
            ))
            continue

        dx, dy = x2-x1, y2-y1
        length = math.hypot(dx, dy)
        if length == 0:
            continue

        # Unit normal to the segment: rotate the direction by 90 deg.
        nx, ny = -dy/length, dx/length
        count = len(colors)

        for i, color in enumerate(colors):
            offset = (i - (count-1)/2) * BAND_WIDTH
            ox, oy = offset*nx, offset*ny
            dwg.add(dwg.line(
                start=(x1+ox, y1+oy),
                end=(x2+ox, y2+oy),
                stroke=color,
                stroke_width=BAND_WIDTH + 0.2,  # Prevent hairline seams
                stroke_linecap="butt",
            ))

    # Draw station-free rounded connections before the station symbols.
    for bend in bends:
        a, b = bend["a"], bend["b"]
        d = arc_path(point(a), point(b), bend["order"])
        if d is None:
            print(f"Warning: bend {a}--{b} has zero horizontal or vertical separation; omitted")
            continue
        dwg.add(dwg.path(d=d, fill="none", stroke=bend["color"],
                         stroke_width=SINGLE_WIDTH, stroke_linecap="round"))

    # Collect the colours of all routes and bends at each station.
    # Stations served by more than one colour are interchanges.
    station_colors = {}

    for line in lines:
        for station in line["stations"]:
            station_colors.setdefault(station, set()).add(line["color"])

    for bend in bends:
        for station in (bend["a"], bend["b"]):
            station_colors.setdefault(station, set()).add(bend["color"])

    # Draw station symbols and labels.
    for station in positions:
        x, y = point(station)
        interchange = len(station_colors.get(station, set())) > 1

        # Interchange stations are larger by default.
        # Replace the default circle radius values 10 and 6 if required.
        dwg.add(dwg.circle(
            center=(x, y),
            r=INTERCHANGE if interchange else LOCAL,
            fill="white",
            stroke="black",
            stroke_width=2.5 if interchange else 2,
        ))

        tx, ty = labels[station]

        # Text colour is defined by the fill option.
        # Use SHOW_LABELS (or fill = "none") to make text invisible.
        if SHOW_LABELS:
            dwg.add(dwg.text(
                station,
                insert=(
                    tx - min_x + MARGIN,
                    ty - min_y + MARGIN
                ),
                font_size="15px",
                font_family="sans-serif",
                fill="black",
            ))

    # Draw the legend below the map.
    if legend:
        legend_y = map_height - MARGIN + legend_gap + 12

        for i, line in enumerate(lines):
            y = legend_y + i * legend_row

            dwg.add(dwg.line(
                start=(MARGIN, y),
                end=(MARGIN + 32, y),
                stroke=line["color"],
                stroke_width=8,
                stroke_linecap="round"
            ))

            dwg.add(dwg.text(
                line["name"],
                insert=(MARGIN + 45, y + 5),
                font_size=f"{FONT_SIZE}px",
                font_family="sans-serif",
                fill="black"
            ))

    dwg.save()

def clash_detect(positions):

    clashes = []
    for (name1, (x1, y1)), (name2, (x2, y2)) in \
            combinations(positions.items(), 2):

        distsq = (x1-x2)**2 + (y1-y2)**2

        if distsq < (SPACING * 0.5)**2:
            clashes.append((name1, name2, distsq**0.5))

    return clashes

def point_segment_distance(p, a, b):
    """Shortest distance from point p to segment a--b."""

    px, py = p
    ax, ay = a
    bx, by = b

    dx = bx - ax
    dy = by - ay

    length_sq = dx*dx + dy*dy

    if length_sq == 0:
        return ((px-ax)**2 + (py-ay)**2)**0.5

    t = ((px-ax)*dx + (py-ay)*dy) / length_sq

    # Keep the projection within the segment.
    t = max(0, min(1, t))

    nearest_x = ax + t*dx
    nearest_y = ay + t*dy

    distance = (
        (px-nearest_x)**2 + (py-nearest_y)**2
    )**0.5

    return distance

def station_line_clashes(lines, positions, min_distance=20):
    """Find stations too close to unrelated line segments."""

    clashes = []

    for line in lines:
        stations = line["stations"]

        for s1, s2 in zip(stations, stations[1:]):

            a = positions[s1]
            b = positions[s2]

            for station, p in positions.items():

                # Stations belonging to this line are allowed.
                if station in stations:
                    continue

                distance = point_segment_distance(p, a, b)

                if distance < min_distance:
                    clashes.append(
                        (station, line["name"], s1, s2, distance)
                    )

    return clashes

def place_labels(lines, positions):
    """Choose label positions without moving stations."""

    FONT_SIZE = 15
    GAP = 14
    DIAG_GAP = 12
    PAD = 3

    # Estimate character widths and label heights.
    def label_size(name):
        return (len(name) * FONT_SIZE * 0.60,
                FONT_SIZE * 1.2)

    def rectangles_overlap(a, b):
        return not (
            a[2] <= b[0] or b[2] <= a[0] or
            a[3] <= b[1] or b[3] <= a[1]
        )

    def segment_hits_rectangle(a, b, r):
        x0, y0, x1, y1 = r

        def inside(p):
            return x0 <= p[0] <= x1 and y0 <= p[1] <= y1

        if inside(a) or inside(b):
            return True

        corners = [
            (x0, y0), (x1, y0),
            (x1, y1), (x0, y1)
        ]

        for p, q in zip(corners, corners[1:] + corners[:1]):
            if segment_intersection(a, b, p, q) is not None:
                return True

        return False

    # Collect all route segments.
    segments = []
    for line in lines:
        s = line["stations"]
        for a, b in zip(s, s[1:]):
            segments.append((positions[a], positions[b]))

    # Place highly connected stations first.
    degree = {name: 0 for name in positions}

    for line in lines:
        s = line["stations"]
        for a, b in zip(s, s[1:]):
            degree[a] += 1
            degree[b] += 1

    ordered = sorted(positions, key=lambda s: -degree[s])

    labels = {}
    occupied = []

    for name in ordered:
        x, y = positions[name]
        w, h = label_size(name)

        candidates = [
            (x+DIAG_GAP,  y-DIAG_GAP-h,   0),  # NE
            (x-w/2,       y-GAP-h,        1),  # N
            (x+GAP,       y-h/2,          2),  # E
            (x-DIAG_GAP-w,y-DIAG_GAP-h,   3),  # NW
            (x-GAP-w,     y-h/2,          4),  # W
            (x-DIAG_GAP-w,y+DIAG_GAP,     5),  # SW
            (x-w/2,       y+GAP,          6),  # S
            (x+DIAG_GAP,  y+DIAG_GAP,     7),  # SE
        ]

        best = None
        best_score = float("inf")

        for left, top, preference in candidates:
            rect = (
                left-PAD, top-PAD,
                left+w+PAD, top+h+PAD
            )

            score = preference * 0.1
            if preference == 2:      # East
                score += 0.2

            # Avoid other labels.
            for other in occupied:
                if rectangles_overlap(rect, other):
                    score += 1000

            # Avoid station symbols, including our own.
            for other, (sx, sy) in positions.items():
                nearest_x = max(rect[0], min(sx, rect[2]))
                nearest_y = max(rect[1], min(sy, rect[3]))

                # These hard-coded limits may need to change if station circles are
                # made larger by adjusting INTERCHANGE or LOCAL
                radius = 10 if other == name else 12

                if ((sx-nearest_x)**2 +
                    (sy-nearest_y)**2 < radius**2):
                    score += 400

            # Avoid coloured track segments.
            for a, b in segments:
                if segment_hits_rectangle(a, b, rect):
                    score += 500

            if score < best_score:
                best_score = score
                best = (left, top, rect)

        left, top, rect = best
        labels[name] = (left, top+h*0.82)
        occupied.append(rect)

    return labels

def geometry_score(lines, positions):

    score = 0

    for line in lines:
        stations = line["stations"]

        for station1, station2 in zip(stations, stations[1:]):

            x1, y1 = positions[station1]
            x2, y2 = positions[station2]

            dx = abs(x2 - x1)
            dy = abs(y2 - y1)

            deviation = min(dx, dy, abs(dx - dy))

            score += (deviation / SPACING)**2

    return score

def diagonal_score(lines, positions):
    score = 0

    for line in lines:
        stations = line["stations"]

        for s1, s2 in zip(stations, stations[1:]):
            x1, y1 = positions[s1]
            x2, y2 = positions[s2]

            if x1 != x2 and y1 != y2:
                score += 1

    return score

def direction_score(lines, positions):
    score = 0

    for line in lines:
        direction = line["direction"]
        stations = line["stations"]

        for s1, s2 in zip(stations, stations[1:]):
            x1, y1 = positions[s1]
            x2, y2 = positions[s2]

            dx = x2 - x1
            dy = y2 - y1

            if direction == "h":
                deviation = abs(dy)
            elif direction == "v":
                deviation = abs(dx)
            elif direction == "d":
                deviation = abs(dx - dy)
            elif direction == "u":
                deviation = abs(dx + dy)

            score += (deviation / SPACING)**2

    return score

def length_score(lines, positions):
    score = 0

    for line in lines:
        stations = line["stations"]

        for s1, s2 in zip(stations, stations[1:]):
            x1, y1 = positions[s1]
            x2, y2 = positions[s2]

            dx = abs(x2 - x1)
            dy = abs(y2 - y1)

            length = max(dx, dy) / SPACING

            score += (length - 1)**2

    return score

def bend_score(lines, positions):
    score = 0

    for line in lines:
        stations = line["stations"]

        for s1, s2, s3 in zip(
            stations, stations[1:], stations[2:]
        ):
            x1, y1 = positions[s1]
            x2, y2 = positions[s2]
            x3, y3 = positions[s3]

            ux, uy = x2-x1, y2-y1
            vx, vy = x3-x2, y3-y2

            cross = ux*vy - uy*vx
            dot = ux*vx + uy*vy

            # Simple bend penalty
            if cross != 0 or dot <= 0:
                score += 1

            # Angle-based diagonal penalty
            #angle = math.atan2(abs(cross), dot)
            #score += (angle / (math.pi / 4))**2

    return score

# bend_score_extra relates to arc segments between stations
def bend_score_extra(positions, a, b):
    x1, y1 = positions[a]
    x2, y2 = positions[b]

    dx = abs(x2 - x1) / SPACING
    dy = abs(y2 - y1) / SPACING

    # Prefer a compact corner: one grid interval on each axis.
    # The difference term still discourages unequal horizontal/vertical legs.
    return (
        (dx - dy)**2
        + (dx - 1)**2
        + (dy - 1)**2
    )

def bend_tangent_score(lines, positions, bends):
    """Prefer route sections that continue smoothly OUT of each arc endpoint.

    Perpendicular distance alone is insufficient: a vertical line can
    approach the endpoint from the wrong side and double back over the arc.
    """
    score = 0
    for bend in bends:
        a, b = bend["a"], bend["b"]
        xa, ya = positions[a]
        xb, yb = positions[b]
        dx, dy = xb-xa, yb-ya

        # A route neighbour must lie *away* from the bend at either end.
        # At a, the route extends opposite to the first leg; at b, it
        # extends along the second leg's direction.
        requirements = (
            (a, bend["order"][0], -1),
            (b, bend["order"][1], +1),
        )
        for station, axis, side in requirements:
            errors = []
            for line in lines:
                if line["color"] != bend["color"]:
                    continue
                names = line["stations"]
                if len(names) < 2:
                    continue
                if station == names[0]:
                    neighbour = names[1]
                elif station == names[-1]:
                    neighbour = names[-2]
                else:
                    continue

                xs, ys = positions[station]
                xn, yn = positions[neighbour]
                if axis == "h":
                    parallel = (xn-xs) / SPACING
                    perpendicular = (yn-ys) / SPACING
                    leg = dx
                else:
                    parallel = (yn-ys) / SPACING
                    perpendicular = (xn-xs) / SPACING
                    leg = dy

                if leg == 0:
                    # Zero-length leg is penalized separately by bend_score_extra.
                    continue
                desired_sign = side * (1 if leg > 0 else -1)
                forward = desired_sign * parallel
                errors.append(perpendicular**2 + max(0, 1-forward)**2)

            if errors:
                score += min(errors)
    return score


def layout_score(lines, positions, bends=()):

    clashes = clash_detect(positions)
    line_clashes = station_line_clashes(lines, positions)

    clash_penalty = WEIGHTS["station"] * len(clashes)
    geometry_penalty = WEIGHTS["geometry"] * geometry_score(lines, positions)
    direction_penalty = WEIGHTS["direction"] * direction_score(lines, positions)
    length_penalty = WEIGHTS["length"] * length_score(lines, positions)
    line_clash_penalty = WEIGHTS["line"] * len(line_clashes)
    bend_penalty = WEIGHTS["bend"] * bend_score(lines, positions)
    crossing_penalty = WEIGHTS["crossing"] * crossing_score(lines, positions)
    diagonal_penalty = WEIGHTS["diagonal"] * diagonal_score(lines, positions)
    tangent_penalty = WEIGHTS["arc_tangent"] * bend_tangent_score(lines, positions, bends)
    arc_penalty = WEIGHTS["arc"] * sum(
        bend_score_extra(positions, bend["a"], bend["b"])
        for bend in bends
    )

    return ( clash_penalty + geometry_penalty + direction_penalty 
            + length_penalty + line_clash_penalty + bend_penalty 
            + crossing_penalty + diagonal_penalty + arc_penalty + tangent_penalty)


def crossing_score(lines, positions):
    """Count unwanted intersections and overlaps."""

    segments = []

    for line in lines:
        stations = line["stations"]

        for s1, s2 in zip(stations, stations[1:]):
            segments.append((
                line["name"], s1, s2,
                positions[s1], positions[s2]
            ))

    score = 0

    for seg1, seg2 in combinations(segments, 2):

        line1, s1, s2, a, b = seg1
        line2, s3, s4, c, d = seg2

        result = segment_intersection(a, b, c, d)

        if result is None:
            continue

        kind, point = result

        # Overlapping routes get a penalty.
        if kind == "overlap":
            score += 2
            continue

        # Is the intersection a genuine shared station?
        shared = {s1, s2} & {s3, s4}

        valid_interchange = any(
            abs(point[0] - positions[s][0]) < 1e-6
            and abs(point[1] - positions[s][1]) < 1e-6
            for s in shared
        )

        if not valid_interchange:
            score += 1

    return score


def save_positions(filename, positions):
    """Save station coordinates as readable text."""

    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Tube station coordinates\n")

        for name, (x, y) in sorted(positions.items()):
            f.write(f"{name}\t{x}\t{y}\n")



def improve_layout(lines, positions, max_steps=100, fixed_stations=None, bends=()):
    """Improve the map by moving stations on a square grid."""

    if fixed_stations is None:
        fixed_stations = set()
    positions = positions.copy()

    best_score = layout_score(lines, positions, bends)
    print(f"Starting score: {best_score:.1f}")

    for step in range(max_steps):

        best_move = None
        new_best_score = best_score

        # Try moving every station.
        for station in positions:
            if station in fixed_stations:
                continue
            x, y = positions[station]
            # Try all eight neighbouring grid positions.
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):

                    if dx == 0 and dy == 0:
                        continue

                    trial = positions.copy()
                    trial[station] = (
                        x + dx * SPACING,
                        y + dy * SPACING
                    )

                    score = layout_score(lines, trial, bends)

                    if score < new_best_score:
                        new_best_score = score
                        best_move = ("move", station, trial[station])

        
        # Also try exchanging the positions of two stations.
        # This can find improvements that single-station
        # movements cannot reach.
        station_names = [
            name for name in positions
            if name not in fixed_stations
        ]

        for s1, s2 in combinations(station_names, 2):

            trial = positions.copy()
            trial[s1], trial[s2] = trial[s2], trial[s1]

            score = layout_score(lines, trial, bends)

            if score < new_best_score:
                new_best_score = score
                best_move = ("swap", s1, s2)

        # Stop when no improving move exists.
        if best_move is None:
            break


        if best_move[0] == "swap":
            _, s1, s2 = best_move

            positions[s1], positions[s2] = (
                positions[s2], positions[s1]
            )

            description = f"swapped {s1} and {s2}"

        else:
            _, station, new_position = best_move
            old_position = positions[station]

            positions[station] = new_position

            description = (
                f"moved {station} "
                f"from {old_position} to {new_position}"
            )

        best_score = new_best_score

        print(
            f"Step {step+1}: {description}, "
            f"score = {best_score:.1f}"
        )

    print(f"Final score: {best_score:.1f}")
    return positions


def anneal_layout(lines, positions, steps=20000,
                  start_temp=2000, end_temp=1, seed=42,
                  fixed_stations=None, bends=()):
    """Improve a layout using simulated annealing."""
    if fixed_stations is None:
        fixed_stations = set()
    rng = random.Random(seed)

    current = positions.copy()
    current_score = layout_score(lines, current, bends)

    best = current.copy()
    best_score = current_score

    #station_names = list(current)
    station_names = [
        name for name in current
        if name not in fixed_stations
    ]

    if not station_names:
        return current

    accepted = 0
    uphill = 0

    for step in range(steps):

        # Exponential cooling schedule.
        fraction = step / max(1, steps - 1)
        temperature = start_temp * (
            end_temp / start_temp
        ) ** fraction

        trial = current.copy()

        if len(station_names) >= 2 and rng.random() < 0.15:
            # Occasionally swap two stations.
            s1, s2 = rng.sample(station_names, 2)
            if s1 in fixed_stations:
                continue
            if s2 in fixed_stations:
                continue
            trial[s1], trial[s2] = trial[s2], trial[s1]

        else:
            # Ordinary station displacement.
            station = rng.choice(station_names)
            if station in fixed_stations:
                continue

            dx, dy = rng.choice([
                (-1, -1), (-1, 0), (-1, 1),
                ( 0, -1),          ( 0, 1),
                ( 1, -1), ( 1, 0), ( 1, 1)
            ])

            distance = rng.choice([1, 1, 1, 2, 2, 3])

            x, y = current[station]

            trial[station] = (
                x + dx * distance * SPACING,
                y + dy * distance * SPACING
            )


        trial_score = layout_score(lines, trial, bends)
        delta = trial_score - current_score

        # Always accept improvements, sometimes accept worsening.
        if delta <= 0 or rng.random() < math.exp(-delta / temperature):

            current = trial
            current_score = trial_score
            accepted += 1

            if delta > 0:
                uphill += 1

            # Remember the best layout seen so far.
            if current_score < best_score:
                best = current.copy()
                best_score = current_score

    print(f"Annealing: {accepted} accepted moves")
    print(f"           {uphill} uphill moves")
    print(f"Best score: {best_score:.1f}")

    return best


def multi_anneal(lines, runs=5, steps=8000, fixed_positions=None, bends=()):
    """Try several independent annealing runs, retaining the best."""

    best_positions = None
    best_score = float("inf")

    fixed_positions = fixed_positions or {}
    fixed_stations = set(fixed_positions)
    if fixed_stations is None:
        fixed_stations = set()

    for run in range(runs):

        # Use a different line as the starting point.
        i = run % len(lines)
        reordered = lines[i:] + lines[:i]

        positions = arrange(reordered, fixed_positions, bends=bends)

        # Independent random sequence for each run.
        positions = anneal_layout(
            lines, positions,
            steps=steps,
            seed=42 + run,
            fixed_stations=fixed_stations, bends=bends
        )

        # Polish the result with greedy optimisation.
        positions = improve_layout(lines, positions,
                        fixed_stations=fixed_stations, bends=bends)

        score = layout_score(lines, positions, bends)

        print(
            f"Run {run+1}/{runs}, "
            f"starting with {reordered[0]['name']}: "
            f"score = {score:.1f}"
        )

        if score < best_score:
            best_score = score
            best_positions = positions.copy()

    print(f"\nBest overall score: {best_score:.1f}")

    return best_positions


def pack_components(lines, positions, gap=2*SPACING, bends=()):
    """Arrange disconnected components side by side."""

    graph = nx.Graph()

    for line in lines:
        stations = line["stations"]
        graph.add_nodes_from(stations)
        graph.add_edges_from(zip(stations, stations[1:]))

    for bend in bends:
        graph.add_edge(bend["a"], bend["b"])
    components = list(nx.connected_components(graph))

    # Make a copy so the original positions remain unchanged.
    packed = positions.copy()

    next_x = 0

    for component in components:

        xs = [positions[s][0] for s in component]
        ys = [positions[s][1] for s in component]

        min_x = min(xs)
        max_x = max(xs)
        min_y = min(ys)

        # Translate the entire component without changing
        # the relative positions of its stations.
        for station in component:
            x, y = positions[station]
            packed[station] = (
                x - min_x + next_x,
                y - min_y
            )

        width = max_x - min_x
        next_x += width + gap

    return packed

def optimise_from_multiple_starts(lines):

    best_positions = None
    best_score = float("inf")

    for i in range(len(lines)):

        reordered = lines[i:] + lines[:i]

        positions = arrange(reordered)
        positions = improve_layout(lines, positions)

        score = layout_score(lines, positions)

        print(f"Starting line {reordered[0]['name']}: score {score:.1f}")

        if score < best_score:
            best_score = score
            best_positions = positions.copy()

    return best_positions

def print_score_breakdown(lines, positions):
    print("\nLayout score breakdown")
    print("----------------------")
    print(f"Station clashes: {len(clash_detect(positions))}")
    print(f"Station-line clashes: {len(station_line_clashes(lines, positions))}")
    print(f"Crossings: {crossing_score(lines, positions)}")
    print(f"Geometry: {geometry_score(lines, positions):.2f}")
    print(f"Direction: {direction_score(lines, positions):.2f}")
    print(f"Length: {length_score(lines, positions):.2f}")
    print(f"Bends: {bend_score(lines, positions)}")
    print(f"Total weighted score: {layout_score(lines, positions):.2f}")


def show_bends(lines, positions):
    for line in lines:
        stations = line["stations"]

        for s1, s2, s3 in zip(
            stations, stations[1:], stations[2:]
        ):
            x1, y1 = positions[s1]
            x2, y2 = positions[s2]
            x3, y3 = positions[s3]

            ux, uy = x2-x1, y2-y1
            vx, vy = x3-x2, y3-y2

            cross = ux*vy - uy*vx
            dot = ux*vx + uy*vy

            if cross != 0 or dot <= 0:
                print(f"{line['name']}: bend at {s2} "
                      f"({s1} -- {s2} -- {s3})")

def load_positions(filename):
    """Read a Tube coordinate file."""

    positions = {}

    with open(filename, encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            name, x, y = line.rsplit(maxsplit=2)
            positions[name] = (int(x), int(y))

    return positions


def main():

    args = sys.argv[1:]

    legend = "--legend" in args
    if legend:
        args.remove("--legend")

    if len(args) not in (1, 2):
        sys.exit("Usage: python tube_v3.py file.tube [file.coords] [--legend]")

    input_file = Path(args[0])
    output_file = input_file.with_suffix(".svg")

    if len(args) == 2:
        fixed_positions = load_positions(args[1])
        print(f"Loaded {len(fixed_positions)} fixed stations")
    else:
        fixed_positions = {}
        print("Creating a new layout")

    lines, bends = read_tube(input_file)
    positions = arrange(lines, fixed_positions, bends=bends)

    DEBUG = False
    if DEBUG:
        print("Positions:", positions)
        print("Clashes:", clash_detect(positions))
        print("Score:", layout_score(lines, positions))
        print("Clashes:", clash_detect(positions))
        print("Geometry:", geometry_score(lines, positions))
        print("Direction:", direction_score(lines, positions))
        print("Length:", length_score(lines, positions))

    # Arrange the network, preserving existing station positions.
    positions = multi_anneal(
        lines,
        runs=5,
        steps=8000,
        fixed_positions=fixed_positions, bends=bends
    )

    # Packing is permitted only for a completely new layout.
    if not fixed_positions:
        positions = pack_components(lines, positions, bends=bends)

    save_file = input_file.with_name(input_file.stem + "_new.coords")
    save_positions(save_file, positions)
    print(f"Saved coordinates to {save_file}")

    draw_map(lines, positions, output_file, legend=legend, bends=bends)
    print(f"Read {len(lines)} lines and {len(positions)} stations")
    print(f"Created {output_file}")


if __name__ == "__main__":
    main()


