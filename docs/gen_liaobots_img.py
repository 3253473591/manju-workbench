# -*- coding: utf-8 -*-
"""liaobots 图片执行器（保底通道 · gpt-image-2.5-flare / sunburst）。

用法:
    python gen_liaobots_img.py [jobs.json] [job_id ...]

作业字段: id / out_name / text / size / model? / quality? / image?（垫图路径）
无 image → 文生图 POST /v1/images/generations（JSON）
有 image → 图生图 POST /v1/images/edits（multipart，垫图锁角色）
每条提示词固定前置 STYLE_PREFIX（保底通道不套本地 LoRA 链，画风全靠这段前缀锁）。
成片下载到 results/；已存在则跳过（不覆盖）。
"""
import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

KEY = "EwI3nntZAWO5R"
BASES = ["https://ai.liaobots.work", "https://ai.liaobots3.work"]
PROXY = "http://127.0.0.1:7890"
MODEL = "gpt-image-2.5-flare"
STYLE_PREFIX = "国漫3D风格，UE5渲染，电影级3D动画, 次表面散射皮肤，避免塑料感。"

HERE = os.path.dirname(os.path.abspath(__file__))
RES_DIR = os.path.join(HERE, "results")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def open_url(req, timeout):
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    return OPENER.open(req, timeout=timeout)


def post_json(url, payload, timeout=900):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with open_url(req, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_edit(url, job, image_path, timeout=900):
    boundary = "----dsh" + uuid.uuid4().hex
    parts = []
    for name, value in (("model", job.get("model", MODEL)),
                        ("prompt", STYLE_PREFIX + job["text"]),
                        ("size", job.get("size", "2048x2048")),
                        ("quality", job.get("quality", "high")),
                        ("n", "1")):
        parts += [f"--{boundary}\r\n".encode(),
                  f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                  str(value).encode("utf-8"), b"\r\n"]
    with open(image_path, "rb") as f:
        blob = f.read()
    parts += [f"--{boundary}\r\n".encode(),
              f'Content-Disposition: form-data; name="image"; filename="{os.path.basename(image_path)}"\r\n'.encode(),
              b"Content-Type: image/png\r\n\r\n", blob, b"\r\n",
              f"--{boundary}--\r\n".encode()]
    req = urllib.request.Request(
        url, data=b"".join(parts), method="POST",
        headers={"Authorization": f"Bearer {KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with open_url(req, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def png_bytes(item):
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with open_url(urllib.request.Request(item["url"]), 900) as r:
            return r.read()
    raise RuntimeError("no image in response item: " + str(item)[:200])


def main():
    args = sys.argv[1:]
    jobs_path = args[0] if args else os.path.join(HERE, "jobs_sz_ep1_k.json")
    want = set(args[1:])
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    os.makedirs(RES_DIR, exist_ok=True)

    for job in jobs:
        if want and job["id"] not in want:
            continue
        dst = os.path.join(RES_DIR, job["out_name"])
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            print(f"[{job['id']}] SKIP exists -> {dst}", flush=True)
            continue
        payload = {
            "model": job.get("model", MODEL),
            "prompt": STYLE_PREFIX + job["text"],
            "size": job.get("size", "2048x2048"),
            "quality": job.get("quality", "high"),
            "n": 1,
        }
        image = job.get("image")
        if image and not os.path.isabs(image):
            image = os.path.join(HERE, image)
        if image:
            print(f"[{job['id']}] {job.get('model', MODEL)} {job.get('size', '?')} "
                  f"EDIT 垫图={os.path.basename(image)} prompt={len(job['text'])}字", flush=True)
        else:
            print(f"[{job['id']}] {job.get('model', MODEL)} {job.get('size', '?')} "
                  f"prompt={len(job['text'])}字", flush=True)
        t0 = time.time()
        done = False
        for base in BASES:
            for attempt in range(3):
                try:
                    if image:
                        resp = post_edit(base + "/v1/images/edits", job, image)
                    else:
                        resp = post_json(base + "/v1/images/generations", payload)
                    data = resp.get("data") or []
                    if not data:
                        print("  no data:", json.dumps(resp, ensure_ascii=False)[:400], flush=True)
                        break
                    blob = png_bytes(data[0])
                    tmp = dst + ".part"
                    with open(tmp, "wb") as f:
                        f.write(blob)
                    os.replace(tmp, dst)
                    print(f"  OK -> {dst}  ({time.time() - t0:.0f}s, {os.path.getsize(dst)}B)", flush=True)
                    done = True
                    break
                except urllib.error.HTTPError as e:
                    print(f"  {base} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}", flush=True)
                    break
                except Exception as e:
                    print(f"  {base} retry {attempt + 1}: {e}", flush=True)
                    time.sleep(5)
            if done:
                break
        if not done:
            print(f"  FAILED: {job['id']}", flush=True)


if __name__ == "__main__":
    main()
