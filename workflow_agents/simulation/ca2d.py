import json
from pathlib import Path
from typing import Dict, Optional

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np
import pandas as pd
from PIL import Image
from workflow_agents.simulation.timing import load_timing
from workflow_agents.simulation.surface_rendering import MAP_RC, basemap_legend, draw_basemap, load_display_layers, prepare_basemap


FLOOD_CMAP = LinearSegmentedColormap.from_list(
    "swmm_2d_flood_warning",
    ["#fff7bc", "#fec44f", "#fd8d3c", "#e31a1c", "#800026"],
)

REQUIRED_MODEL_FILES = [
    "config.json",
    "elevation.npy",
    "smid_grid.npy",
    "flow_mask.npy",
    "building_mask.npy",
    "resistance.npy",
    "node_to_cell_mapping.csv",
]


def check_static_model(model_dir: Path) -> Dict[str, object]:
    missing = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).exists()]
    if missing:
        return {"ok": False, "model_dir": str(model_dir), "missing": missing}

    config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
    elevation = np.load(model_dir / "elevation.npy")
    node_mapping = pd.read_csv(model_dir / "node_to_cell_mapping.csv", dtype={"node_id": str})
    return {
        "ok": True,
        "model_dir": str(model_dir),
        "grid_shape": list(elevation.shape),
        "cell_size": float(config["cell_size"]),
        "mapped_nodes": int(len(node_mapping)),
        "flow_cells": int(np.load(model_dir / "flow_mask.npy").sum()),
    }


def load_model(model_dir: Path) -> Dict[str, object]:
    status = check_static_model(model_dir)
    if not status["ok"]:
        raise FileNotFoundError(
            f"CA2D static model is incomplete: {model_dir}. Missing: {', '.join(status['missing'])}"
        )
    model = {
        "config": json.loads((model_dir / "config.json").read_text(encoding="utf-8")),
        "elevation": np.load(model_dir / "elevation.npy"),
        "smid_grid": np.load(model_dir / "smid_grid.npy"),
        "flow_mask": np.load(model_dir / "flow_mask.npy"),
        "building_mask": np.load(model_dir / "building_mask.npy"),
        "resistance": np.load(model_dir / "resistance.npy"),
        "node_mapping": pd.read_csv(model_dir / "node_to_cell_mapping.csv", dtype={"node_id": str}),
    }
    model["display_layers"] = load_display_layers(
        model_dir, model["config"], model["elevation"], model["flow_mask"], model["building_mask"])
    return model


def read_node_outflow_data(flooding_file: Path) -> pd.DataFrame:
    raw = pd.read_csv(flooding_file, sep=r"\s+|\t|,", header=None, engine="python", dtype=str)
    if raw.shape[1] < 4:
        raise ValueError("Flooding file must contain node_id, date, time, and flow rate columns.")

    raw = raw.iloc[:, :4]
    raw.columns = ["node_id", "Date", "Time", "Flow_Rate_Ls"]
    if raw.iloc[0]["node_id"].lower() in {"node", "node_id", "id"}:
        raw = raw.iloc[1:].copy()

    raw["node_id"] = raw["node_id"].str.strip()
    raw["Flow_Rate_Ls"] = pd.to_numeric(raw["Flow_Rate_Ls"], errors="coerce").fillna(0.0)
    raw["DateTime"] = pd.to_datetime(raw["Date"] + " " + raw["Time"], errors="coerce")
    raw = raw.dropna(subset=["DateTime"])
    raw["Flow_Rate_m3s"] = raw["Flow_Rate_Ls"] / 1000.0
    return raw.sort_values(["DateTime", "node_id"]).reset_index(drop=True)


def prepare_inflow_by_time(flooding_df: pd.DataFrame, node_mapping: pd.DataFrame):
    required = {"node_id", "cell_row", "cell_col", "weight"}
    missing = required - set(node_mapping.columns)
    if missing:
        raise ValueError(f"node_to_cell_mapping.csv missing columns: {sorted(missing)}")

    node_mapping = node_mapping.copy()
    node_mapping["node_id"] = node_mapping["node_id"].astype(str).str.strip()
    merged = flooding_df.merge(
        node_mapping[["node_id", "cell_row", "cell_col", "weight"]],
        on="node_id",
        how="inner",
    )
    if merged.empty:
        raise ValueError("No SWMM flooding nodes matched node_to_cell_mapping.csv.")

    merged["weighted_flow_m3s"] = merged["Flow_Rate_m3s"] * pd.to_numeric(
        merged["weight"], errors="coerce"
    ).fillna(1.0)
    grouped = (
        merged.groupby(["DateTime", "cell_row", "cell_col"], as_index=False)["weighted_flow_m3s"]
        .sum()
        .sort_values("DateTime")
    )
    return grouped, int(merged["node_id"].nunique())


def interpolate_inflow_by_time(inflow_by_time: pd.DataFrame, interval_minutes: float, method="point"):
    if not np.isfinite(interval_minutes) or interval_minutes <= 0:
        raise ValueError("Boundary interval must be positive and finite")
    report_times = sorted(pd.Timestamp(value) for value in inflow_by_time["DateTime"].unique())
    if len(report_times) < 2:
        raise ValueError("Flooding time series needs at least two timestamps.")

    start_time = report_times[0]
    end_time = report_times[-1]
    boundary_times = list(pd.date_range(start=start_time, end=end_time, freq=f"{interval_minutes}min"))
    if boundary_times[-1] != end_time:
        boundary_times.append(end_time)

    pivot = (
        inflow_by_time.pivot_table(
            index="DateTime",
            columns=["cell_row", "cell_col"],
            values="weighted_flow_m3s",
            aggfunc="sum",
            fill_value=0.0,
        )
        .sort_index()
    )
    full_index = pd.DatetimeIndex(sorted(set(pivot.index).union(boundary_times)))
    expanded = (
        pivot.reindex(full_index)
        .interpolate(method="time")
        .ffill()
        .bfill()
    )
    if method == "interval_mean":
        # Integrate the source hydrograph BEFORE reducing its temporal density.
        # The last timestamp is an endpoint, never a further input interval.
        values = expanded.to_numpy(dtype=float)
        seconds = (full_index[1:] - full_index[:-1]).total_seconds().to_numpy()
        cumulative = np.vstack([np.zeros(values.shape[1]),
                                np.cumsum((values[:-1] + values[1:]) * 0.5 * seconds[:, None], axis=0)])
        positions = full_index.get_indexer(pd.DatetimeIndex(boundary_times))
        volumes = np.diff(cumulative[positions], axis=0)
        boundary_index = pd.DatetimeIndex(boundary_times)
        durations = (boundary_index[1:] - boundary_index[:-1]).total_seconds().to_numpy()
        interpolated = pd.DataFrame(volumes / durations[:, None],
                                    index=boundary_times[:-1], columns=pivot.columns)
    elif method == "point":
        interpolated = expanded.reindex(pd.DatetimeIndex(boundary_times))
    else:
        raise ValueError(f"Unknown boundary method: {method}")

    rows = []
    for dt_value, row in interpolated.iterrows():
        nonzero = row[row > 0]
        for (cell_row, cell_col), flow in nonzero.items():
            rows.append(
                {
                    "DateTime": pd.Timestamp(dt_value),
                    "cell_row": int(cell_row),
                    "cell_col": int(cell_col),
                    "weighted_flow_m3s": float(flow),
                }
            )
    # Preserve the schema when SWMM has no positive overflow; callers can then
    # return a domain result instead of leaking KeyError('DateTime').
    return pd.DataFrame(rows, columns=["DateTime", "cell_row", "cell_col", "weighted_flow_m3s"]), report_times, boundary_times


def add_inflow(depth, rows, cols, flows_m3s, dt, cell_size):
    if len(rows) == 0:
        return 0.0
    depth_add = flows_m3s * dt / (cell_size * cell_size)
    np.add.at(depth, (rows, cols), depth_add.astype(np.float32))
    return float(np.sum(flows_m3s) * dt)


def diffuse_one_step(depth, dem, resistance, flow_mask, dt, cell_size, k=0.58, max_out_fraction=0.18):
    water_level = dem + depth
    total_weight = np.zeros_like(depth, dtype=np.float32)
    direction_weights = []

    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        shifted_level = np.roll(water_level, shift=(-dr, -dc), axis=(0, 1))
        shifted_resistance = np.roll(resistance, shift=(-dr, -dc), axis=(0, 1))
        shifted_flow_mask = np.roll(flow_mask, shift=(-dr, -dc), axis=(0, 1))

        level_diff = water_level - shifted_level
        pair_resistance = 0.5 * (resistance + shifted_resistance)
        weight = np.where(
            (level_diff > 0) & flow_mask & shifted_flow_mask & (depth > 1e-7),
            pair_resistance * level_diff / cell_size,
            0.0,
        ).astype(np.float32)

        if dr == -1:
            weight[0, :] = 0.0
        elif dr == 1:
            weight[-1, :] = 0.0
        elif dc == -1:
            weight[:, 0] = 0.0
        elif dc == 1:
            weight[:, -1] = 0.0

        direction_weights.append((dr, dc, weight))
        total_weight += weight

    desired = k * (dt / cell_size) * total_weight
    transfer_total = np.minimum(depth * max_out_fraction, desired)
    transfer_total = np.where(total_weight > 0, transfer_total, 0.0).astype(np.float32)

    delta = -transfer_total
    for dr, dc, weight in direction_weights:
        share = np.zeros_like(weight, dtype=np.float32)
        np.divide(weight, total_weight, out=share, where=total_weight > 0)
        outgoing = transfer_total * share
        incoming = np.roll(outgoing, shift=(dr, dc), axis=(0, 1))

        if dr == -1:
            incoming[-1, :] = 0.0
        elif dr == 1:
            incoming[0, :] = 0.0
        elif dc == -1:
            incoming[:, -1] = 0.0
        elif dc == 1:
            incoming[:, 0] = 0.0
        delta += incoming

    depth += delta
    np.maximum(depth, 0.0, out=depth)
    depth[~flow_mask] = 0.0
    depth[0, :] = 0.0
    depth[-1, :] = 0.0
    depth[:, 0] = 0.0
    depth[:, -1] = 0.0


def frame_to_records(depth, smid_grid, dt_value):
    mask = smid_grid > 0
    return pd.DataFrame(
        {
            "Smid": smid_grid[mask].astype(np.int32),
            "Date": dt_value.strftime("%Y-%m-%d"),
            "Time": dt_value.strftime("%H:%M:%S"),
            "Depth": np.round(depth[mask].astype(np.float32), 3),
        }
    )


def _depth_figure(depth, flow_mask, layers, title, subtitle, vmax, dpi):
    # Preserve the selected preview's portrait layout for tall urban domains,
    # while giving wide model domains enough room for their own aspect ratio.
    ny, nx = depth.shape
    width = max(6.3, 8.0 * nx / max(ny, 1))
    fig = plt.figure(figsize=(width, 8.0), dpi=dpi)
    ax = fig.add_axes([.055, .14, .79, .73])
    draw_basemap(ax, layers)
    image = ax.imshow(np.ma.masked_where((depth <= .005) | (~flow_mask), depth),
                      cmap=FLOOD_CMAP, vmin=0, vmax=vmax, alpha=.94,
                      interpolation="nearest", zorder=8)
    fig.text(.5, .96, title, ha="center", fontsize=16, color="#26342d")
    stamp = fig.text(.5, .914, subtitle, ha="center", fontsize=11, color="#707b74")
    handles = basemap_legend(layers["road_known"])
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(.5, .065),
               ncol=len(handles), frameon=False, fontsize=9)
    cbar = fig.colorbar(image, cax=fig.add_axes([.887, .245, .021, .50]))
    cbar.set_label("水深（m）", fontsize=10)
    cbar.outline.set_edgecolor("#bcc1bd")
    return fig, image, stamp


def render_depth(depth, elevation, flow_mask, building_mask, output_path, title, **display_layers):
    layers = prepare_basemap(elevation, flow_mask, building_mask, **display_layers)
    with plt.rc_context(MAP_RC):
        fig, _, _ = _depth_figure(depth, flow_mask, layers, title, "", max(.35, float(np.nanmax(depth))), 150)
        try:
            fig.savefig(output_path)
        finally:
            plt.close(fig)


def make_gif(frames, elevation, flow_mask, building_mask, output_path, **display_layers):
    if not frames:
        return
    layers = prepare_basemap(elevation, flow_mask, building_mask, **display_layers)
    images = []
    vmax = max(0.35, max(float(np.nanmax(frame)) for _time, frame in frames))
    with plt.rc_context(MAP_RC):
        fig, image, stamp = _depth_figure(frames[0][1], flow_mask, layers, "地表积水演变", "", vmax, 120)
        try:
            for dt_value, frame in frames:
                image.set_data(np.ma.masked_where((frame <= .005) | (~flow_mask), frame))
                stamp.set_text(dt_value.strftime("%Y-%m-%d  %H:%M:%S"))
                fig.canvas.draw()
                rgb = np.asarray(fig.canvas.buffer_rgba())[..., :3].copy()
                images.append(Image.fromarray(rgb).convert("P", palette=Image.Palette.ADAPTIVE, colors=256))
            images[0].save(output_path, save_all=True, append_images=images[1:], duration=150, loop=0)
        finally:
            plt.close(fig)
            for image in images:
                image.close()


def write_tif(depth, config, output_path):
    try:
        import rasterio
        from rasterio.transform import from_origin
    except ModuleNotFoundError:
        return False

    minx, _miny, _maxx, maxy = config["bounds"]
    cell_size = config["cell_size"]
    transform = from_origin(minx, maxy, cell_size, cell_size)
    with rasterio.open(
        output_path,
        "w",
        driver="GTiff",
        height=depth.shape[0],
        width=depth.shape[1],
        count=1,
        dtype="float32",
        crs=config.get("crs"),
        transform=transform,
        nodata=0.0,
        compress="deflate",
    ) as dst:
        dst.write(depth.astype("float32"), 1)
    return True


def run_ca2d_simulation(
    model_dir: Path,
    flooding_file: Path,
    output_dir: Path,
    dt_seconds: float = 10.0,
    boundary_interval_minutes: float = 5.0,
    save_interval_minutes: float = 30.0,
    output_txt_path: Optional[Path] = None,
) -> Dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    model = load_model(model_dir)
    config = model["config"]
    cell_size = float(config["cell_size"])
    timing = load_timing(Path(model_dir).parent)
    if timing:
        boundary_interval_minutes = float(timing["boundary_interval_minutes"])
        save_interval_minutes = float(timing["ca2d_save_interval_minutes"])
    method = timing.get("boundary_method", "point")
    if not np.isfinite(dt_seconds) or dt_seconds <= 0:
        raise ValueError("CA2D internal time step must be positive and finite")

    flooding_df = read_node_outflow_data(flooding_file)
    inflow_by_time, matched_nodes = prepare_inflow_by_time(flooding_df, model["node_mapping"])
    boundary_inflow_by_time, report_times, boundary_times = interpolate_inflow_by_time(
        inflow_by_time,
        boundary_interval_minutes,
        method=method,
    )
    # Zero overflow is a valid hydraulic result.  Continue with an all-zero
    # surface boundary so the normal CA2D outputs document zero inundation.
    surface_inflow_status = "NO_SURFACE_INFLOW" if boundary_inflow_by_time.empty else "POSITIVE_SURFACE_INFLOW"
    save_time_set = set(pd.date_range(start=report_times[0], end=report_times[-1], freq=f"{save_interval_minutes}min"))
    if timing:
        save_time_set.add(report_times[-1])
    else:
        save_time_set.update(report_times)

    elevation = model["elevation"]
    depth = np.zeros_like(elevation, dtype=np.float32)
    max_depth = np.zeros_like(elevation, dtype=np.float32)
    records = []
    gif_frames = []
    input_volume = 0.0

    if timing:
        # Save states at their actual time, starting with the initial condition.
        # Union of output and boundary clocks supports independent save intervals.
        evolution_times = sorted(set(boundary_times).union(save_time_set))
        records.append(frame_to_records(depth, model["smid_grid"], evolution_times[0]))
        gif_frames.append((evolution_times[0], depth.copy()))
        boundary_lookup = {t: rows for t, rows in boundary_inflow_by_time.groupby("DateTime")}
        empty = boundary_inflow_by_time.iloc[:0]
        boundary_index = 0
        for current_time, next_time in zip(evolution_times[:-1], evolution_times[1:]):
            while boundary_index + 1 < len(boundary_times) and boundary_times[boundary_index + 1] <= current_time:
                boundary_index += 1
            current_rows = boundary_lookup.get(boundary_times[boundary_index], empty)
            duration_seconds = (next_time - current_time).total_seconds()
            n_steps = max(1, int(np.ceil(duration_seconds / dt_seconds)))
            local_dt = duration_seconds / n_steps
            rows = current_rows["cell_row"].to_numpy(dtype=np.int32)
            cols = current_rows["cell_col"].to_numpy(dtype=np.int32)
            flows = current_rows["weighted_flow_m3s"].to_numpy(dtype=np.float64)
            for _ in range(n_steps):
                input_volume += add_inflow(depth, rows, cols, flows, local_dt, cell_size)
                diffuse_one_step(depth, elevation, model["resistance"], model["flow_mask"], local_dt, cell_size)
                np.maximum(max_depth, depth, out=max_depth)
            if next_time in save_time_set:
                records.append(frame_to_records(depth, model["smid_grid"], next_time))
                gif_frames.append((next_time, depth.copy()))
        boundary_export = boundary_inflow_by_time.copy()
        ends = dict(zip(boundary_times[:-1], boundary_times[1:]))
        boundary_export["interval_end"] = boundary_export["DateTime"].map(ends)
        boundary_export["volume_m3"] = [
            (ends[t] - t).total_seconds() * q
            for t, q in zip(boundary_export["DateTime"], boundary_export["weighted_flow_m3s"])]
        boundary_export.to_csv(output_dir / "boundary_inflow_5min.tsv", sep="\t", index=False)

    for time_index, current_time in enumerate([] if timing else boundary_times):
        current_time = pd.Timestamp(current_time)
        current_rows = boundary_inflow_by_time.loc[boundary_inflow_by_time["DateTime"] == current_time]
        next_time = boundary_times[time_index + 1] if time_index + 1 < len(boundary_times) else current_time
        duration_seconds = max(0.0, (pd.Timestamp(next_time) - current_time).total_seconds())
        n_steps = max(1, int(np.ceil(duration_seconds / dt_seconds))) if duration_seconds > 0 else 1
        local_dt = duration_seconds / n_steps if duration_seconds > 0 else dt_seconds

        rows = current_rows["cell_row"].to_numpy(dtype=np.int32)
        cols = current_rows["cell_col"].to_numpy(dtype=np.int32)
        flows = current_rows["weighted_flow_m3s"].to_numpy(dtype=np.float32)

        for _ in range(n_steps):
            input_volume += add_inflow(depth, rows, cols, flows, local_dt, cell_size)
            diffuse_one_step(depth, elevation, model["resistance"], model["flow_mask"], local_dt, cell_size)
            np.maximum(max_depth, depth, out=max_depth)

        frame_depth = depth.copy()
        gif_frames.append((current_time, frame_depth))
        if current_time in save_time_set:
            records.append(frame_to_records(frame_depth, model["smid_grid"], current_time))

    result_df = pd.concat(records, ignore_index=True)
    result_txt = output_txt_path or (output_dir / "swmm_2d_surface_depth.tsv")
    result_df.to_csv(result_txt, sep="\t", index=False, encoding="utf-8")

    max_depth_png = output_dir / "ca2d_max_depth.png"
    final_depth_png = output_dir / "ca2d_final_depth.png"
    animation_gif = output_dir / "ca2d_animation.gif"
    max_depth_tif = output_dir / "ca2d_max_depth.tif"
    display_layers = model.get("display_layers", {})
    render_depth(max_depth, elevation, model["flow_mask"], model["building_mask"], max_depth_png, "地表最大水深", **display_layers)
    render_depth(depth, elevation, model["flow_mask"], model["building_mask"], final_depth_png, "模拟结束时水深", **display_layers)
    make_gif(gif_frames, elevation, model["flow_mask"], model["building_mask"], animation_gif, **display_layers)
    tif_written = write_tif(max_depth, config, max_depth_tif)

    summary = {
        "status": "COMPLETED",
        "surface_inflow_status": surface_inflow_status,
        "model_dir": str(model_dir),
        "flooding_file": str(flooding_file),
        "output_dir": str(output_dir),
        "result_txt": str(result_txt),
        "max_depth_png": str(max_depth_png),
        "final_depth_png": str(final_depth_png),
        "animation_gif": str(animation_gif),
        "max_depth_tif": str(max_depth_tif) if tif_written else None,
        "flooding_nodes": int(flooding_df["node_id"].nunique()),
        "matched_nodes": matched_nodes,
        "report_times": len(report_times),
        "boundary_times": len(boundary_times),
        "boundary_interval_minutes": float(boundary_interval_minutes),
        "save_interval_minutes": float(save_interval_minutes),
        "dt_seconds": float(dt_seconds),
        "boundary_method": method,
        "simulation_start": report_times[0].isoformat(sep=" "),
        "simulation_end": report_times[-1].isoformat(sep=" "),
        "saved_times": len(records),
        "source_volume_m3": float(boundary_export["volume_m3"].sum()) if timing else None,
        "coupling_volume_error_m3": float(input_volume - boundary_export["volume_m3"].sum()) if timing else None,
        "input_volume_m3": float(input_volume),
        "max_depth_m": float(np.nanmax(max_depth)),
        "final_volume_m3": float(np.nansum(depth) * cell_size * cell_size),
        "result_records": int(len(result_df)),
        "tif_written": bool(tif_written),
    }
    write_run_summary(output_dir / "ca2d_run_summary.md", summary)
    return summary


def write_run_summary(path: Path, summary: Dict[str, object]) -> None:
    lines = [
        "# SWMM-2D CA2D Run Summary",
        "",
        f"- Model directory: `{summary['model_dir']}`",
        f"- Flooding file: `{summary['flooding_file']}`",
        f"- Output directory: `{summary['output_dir']}`",
        f"- Flooding nodes: {summary['flooding_nodes']}",
        f"- Matched nodes: {summary['matched_nodes']}",
        f"- Report timestamps: {summary['report_times']}",
        f"- Boundary timestamps: {summary['boundary_times']}",
        f"- CA2D time step: {summary['dt_seconds']} s",
        f"- Input volume: {summary['input_volume_m3']:.3f} m3",
        f"- Maximum depth: {summary['max_depth_m']:.4f} m",
        f"- Final volume: {summary['final_volume_m3']:.3f} m3",
        f"- Result records: {summary['result_records']}",
        "",
        "## Outputs",
        "",
        f"- Depth table: `{summary['result_txt']}`",
        f"- Max-depth PNG: `{summary['max_depth_png']}`",
        f"- Final-depth PNG: `{summary['final_depth_png']}`",
        f"- Animation GIF: `{summary['animation_gif']}`",
        f"- GeoTIFF written: {summary['tif_written']}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
