"""Model-local timing policy and the current native SWMM result exporter."""
import json
import math
from pathlib import Path


def load_timing(model_root, *, required=False):
    path = Path(model_root) / "simulation_timing.json"
    if not path.exists():
        if required:
            raise FileNotFoundError(
                f"Missing required simulation timing configuration: {path}. "
                "Add this model's simulation_timing.json before starting a rainfall run."
            )
        return {}
    policy = json.loads(path.read_text(encoding="utf-8"))
    for key in ("swmm_save_interval_minutes", "boundary_interval_minutes", "ca2d_save_interval_minutes"):
        value = float(policy[key])
        if not math.isfinite(value) or value <= 0 or value * 60 != int(value * 60):
            raise ValueError(f"Invalid timing policy: {key}")
    tail = float(policy["post_rainfall_hours"])
    if not math.isfinite(tail) or tail < 0:
        raise ValueError("post_rainfall_hours must be finite and nonnegative")
    if policy.get("boundary_method") != "interval_mean":
        raise ValueError("The timing policy requires interval_mean boundaries")
    if policy.get("rainfall_time_convention") != "interval_start":
        raise ValueError("The timing policy requires interval_start rainfall timestamps")
    return policy


def export_native_reports(inp_path, output_dir, mapped_ids, interval_minutes):
    """Use native report timestamps; retain initial/final states for integration.

    Routing settings are not modified. Coupling subsequently integrates the
    piecewise-linear one-minute reports (not a routing-step water budget).
    """
    from pyswmm import Simulation, Nodes, Links, Output
    from swmm.toolkit.shared_enum import NodeAttribute, LinkAttribute

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rpt, out = output_dir / "model.rpt", output_dir / "model.out"
    total_steps = 0
    with Simulation(str(inp_path), str(rpt), str(out)) as sim:
        nodes = {n.nodeid: n for n in Nodes(sim)}
        links = {l.linkid: l for l in Links(sim)}
        if sim.flow_units != "LPS":
            raise ValueError("The model-local timing exporter currently requires FLOW_UNITS LPS")
        sim.start()
        start, end = sim.start_time, sim.end_time

        def snapshot():
            return ({k: n.depth for k, n in nodes.items()},
                    {k: n.flooding for k, n in nodes.items()},
                    {k: l.flow for k, l in links.items()},
                    {k: l.depth for k, l in links.items()})

        initial = snapshot()
        for _ in sim:
            total_steps += 1
        final = snapshot()

    paths = {"flooding_file": output_dir / "node_flooding.tsv",
             "nodes_file": output_dir / "nodes.tsv", "links_file": output_dir / "links.tsv"}
    count = 0
    saved = 0
    peak_flow = peak_depth = 0.0
    with paths["flooding_file"].open("w", encoding="utf-8") as ff, \
         paths["nodes_file"].open("w", encoding="utf-8") as nf, \
         paths["links_file"].open("w", encoding="utf-8") as lf:
        ff.write("node_id\tdate\ttime\tflow_Ls\n")
        nf.write("node_id\tdepth_m\tflooding_Ls\tdate\ttime\n")
        lf.write("link_id\tflow_Ls\tdepth_m\tdate\ttime\n")

        def write(time, state):
            nonlocal count, saved, peak_flow, peak_depth
            depths, floods, flows, link_depths = state
            stamp = time.strftime("%Y-%m-%d\t%H:%M:%S")
            for node_id in depths:
                nf.write(f"{node_id}\t{depths[node_id]:.9g}\t{floods[node_id]:.9g}\t{stamp}\n")
                peak_flow = max(peak_flow, floods[node_id])
                peak_depth = max(peak_depth, depths[node_id])
                if not mapped_ids or node_id in mapped_ids:
                    ff.write(f"{node_id}\t{stamp}\t{floods[node_id]:.9g}\n")
                    count += 1
            for link_id in flows:
                lf.write(f"{link_id}\t{flows[link_id]:.9g}\t{link_depths[link_id]:.9g}\t{stamp}\n")
            saved += 1

        write(start, initial)
        with Output(str(out)) as output:
            if output.report != int(interval_minutes * 60):
                raise ValueError("Native SWMM REPORT_STEP does not match model timing policy")
            last = start
            for index, time in enumerate(output.times):
                if not start < time <= end:
                    continue
                state = (output.node_attribute(NodeAttribute.INVERT_DEPTH, index),
                         output.node_attribute(NodeAttribute.FLOODING_LOSSES, index),
                         output.link_attribute(LinkAttribute.FLOW_RATE, index),
                         output.link_attribute(LinkAttribute.FLOW_DEPTH, index))
                write(time, state)
                last = time
            if last < end:
                write(end, final)
    return {**{k: str(v) for k, v in paths.items()}, "modified_inp": str(inp_path),
            "report_file": str(rpt), "binary_output_file": str(out),
            "total_steps": total_steps, "saved_steps": saved,
            "node_count": len(nodes), "link_count": len(links), "flooding_records": count,
            "max_flooding_Ls": peak_flow, "max_node_depth_m": peak_depth,
            "save_interval_minutes": interval_minutes,
            "simulation_start": start.isoformat(sep=" "), "simulation_end": end.isoformat(sep=" "),
            "sampling_method": "native_report_with_initial_and_final_states"}
