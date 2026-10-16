# Usage: run_sample.py [RUN_ID]   (same RUN_ID resumes: saved plan + finished shots reused, pending fal/Veo
# requests fetched by id; nothing is re-planned or regenerated). Never publishes.
import json, logging, sys
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
import stitched_shorts as ss
settings = {"gemini_api_key": open("/home/box/.secrets/gemini_api_key_2026-10-07").read().strip(),
            "fal_api_key": open("/home/box/.secrets/fal_api_key").read().strip(),
            "shorts_test_mode": True, "shorts_cost_cap_usd": 0.75, "shorts_forced_durations": [4, 5, 5, 5]}
run_id = sys.argv[1] if len(sys.argv) > 1 else None
out, plan, info = ss.produce_short(settings, "/workspace/shorts-sample", run_id=run_id,
                                   runs_dir="/workspace/shorts-sample/runs")
json.dump({"file": out, "plan": plan, "info": info}, open(f"/workspace/shorts-sample/sample_record_{info['run_id']}.json", "w"), indent=2, default=str)
print("DONE", info["run_id"], out, info["duration"], info["width"], info["height"], info["cost_usd"])
