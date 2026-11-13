# -*- coding: utf-8 -*-
"""
B 站视频「指定时间段 BGM」识别核心逻辑（供 Streamlit 网页调用）。

对外主要接口：
  parse_time(s)           解析 "1:30" / "90" / "1分30秒" -> 秒数
  identify(bvid, start, end, use_vocal_removal, progress)
                         识别某视频 [start, end] 秒区间内的 BGM
                         返回 dict: {"title","artist","used_vocal_removal","note"}

识别流程：
  1. WBI 签名获取播放地址 -> 下载区间所需音频（仅 0 ~ end+5 秒）
  2. 用 ffmpeg 切出 [start, end] 区间
  3. 直接交给 Shazam 识别；若被解说/对话盖住识别失败，且用户开启了
     “去人声”，则自动用 Demucs 分离人声后再识别一次
"""
import os
import re
import time
import hashlib
import json
import asyncio
import subprocess
import tempfile
import shutil

import httpx
from shazamio import Shazam

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_FF = None


def get_ffmpeg():
    """优先用系统 ffmpeg（Streamlit Cloud 通过 packages.txt 安装），
    否则回退到 imageio-ffmpeg 提供的二进制。"""
    global _FF
    if _FF and os.path.exists(_FF):
        return _FF
    p = shutil.which("ffmpeg")
    if p:
        _FF = p
        return _FF
    try:
        import imageio_ffmpeg
        _FF = imageio_ffmpeg.get_ffmpeg_exe()
        return _FF
    except Exception:
        _FF = "ffmpeg"
        return _FF


# ---------- B 站接口：WBI 签名 ----------
def wbi_sign():
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    nav = httpx.get("https://api.bilibili.com/x/web-interface/nav",
                    headers=h, timeout=15).json()
    img = nav["data"]["wbi_img"]["img_url"].split("/")[-1].split(".")[0]
    sub = nav["data"]["wbi_img"]["sub_url"].split("/")[-1].split(".")[0]
    raw = img + sub
    TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5,
             49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55,
             40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57,
             62, 11, 36, 20, 34, 44, 52]
    mixin = "".join(raw[i] for i in TABLE[:32])

    def sign(params):
        params = dict(params)
        params["wts"] = int(time.time())
        qs = "&".join(f"{k}={urllib_parse(params[k])}" for k in sorted(params))
        params["w_rid"] = hashlib.md5((qs + mixin).encode()).hexdigest()
        return params

    return sign, h


def urllib_parse(v):
    # 与 httpx 的 urlencode 行为保持一致（对中文/特殊字符编码）
    from urllib.parse import quote
    return quote(str(v), safe="")


# ---------- BV / AV 解析 ----------
def resolve_bvid(raw, h):
    raw = raw.strip()
    if re.match(r"(?i)bv", raw):
        return raw
    m = re.search(r"\d+", raw)
    if not m:
        return raw
    aid = m.group(0)
    v = httpx.get("https://api.bilibili.com/x/web-interface/view",
                  params={"aid": aid}, headers=h, timeout=15).json()
    return v["data"]["bvid"]


def get_audio_url(bvid, sign, h):
    v = httpx.get("https://api.bilibili.com/x/web-interface/view",
                  params={"bvid": bvid}, headers=h, timeout=15).json()
    cid = v["data"]["cid"]
    p = sign({"bvid": bvid, "cid": cid, "qn": "16", "fnval": "16",
              "fnver": "0", "fourk": "1", "platform": "pc",
              "web_location": 1315875})
    r = httpx.get("https://api.bilibili.com/x/player/wbi/playurl",
                  params=p, headers=h, timeout=15).json()
    audio = r["data"]["dash"]["audio"][0]
    return audio["baseUrl"], cid, audio.get("bandwidth", 128000)


# ---------- 下载 / 切片 ----------
def download_audio(url, h, max_bytes=None):
    if max_bytes:
        hh = dict(h)
        hh["Range"] = f"bytes=0-{max_bytes}"
        resp = httpx.get(url, headers=hh, timeout=120)
    else:
        resp = httpx.get(url, headers=h, timeout=600)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".m4s")
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def get_duration(path):
    ff = get_ffmpeg()
    out = subprocess.run([ff, "-i", path], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if m:
        h_, m_, s_ = map(float, m.groups())
        return h_ * 3600 + m_ * 60 + s_
    return None


def extract_clip(full, start, end, out_path):
    ff = get_ffmpeg()
    dur = end - start
    if dur <= 0:
        return None
    subprocess.run([ff, "-y", "-ss", f"{start:.3f}", "-i", full, "-t", f"{dur:.3f}",
                    "-vn", "-ac", "1", "-ar", "44100", out_path],
                   capture_output=True)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 2000:
        return None
    return out_path


# ---------- 人声分离 ----------
def demucs_available():
    try:
        import importlib.util
        return importlib.util.find_spec("demucs") is not None
    except Exception:
        return False


def remove_vocals(mp3, workdir):
    """用 Demucs 分离人声，返回去人声后的 wav 路径；不可用则返回 None。

    直接调用 demucs 的库 API（get_model + apply_model），避免 demucs 自带
    CLI (separate.py) 在 torch>=2.0 下因原地运算报错的问题。
    """
    if not demucs_available():
        return None
    import torch
    import torchaudio
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
    from demucs.audio import convert_audio

    # 读取音频（本工具喂入的切片是 wav，走 soundfile 后端）
    wav, sr = torchaudio.load(mp3)
    device = "cpu"
    model = get_model("htdemucs")
    model.to(device).eval()

    # 转成模型所需的声道数 / 采样率
    wav = convert_audio(wav, sr, model.samplerate, model.audio_channels)

    # 归一化（非原地，兼容新版 torch）
    ref = wav.mean(0)
    mean = ref.mean()
    std = ref.std()
    wav = (wav - mean) / std

    with torch.no_grad():
        sources = apply_model(model, wav[None], device=device, progress=False)[0]

    # two-stems=vocals: no_vocals = 除 vocals 外所有源之和
    vocals_idx = model.sources.index("vocals")
    no_vocals = sources.sum(0) - sources[vocals_idx]
    no_vocals = no_vocals * std + mean  # 反归一化
    no_vocals = torch.clamp(no_vocals, -1.0, 1.0)

    dst = os.path.join(workdir, "_novocal.wav")
    torchaudio.save(dst, no_vocals.cpu(), model.samplerate)
    return dst


# ---------- 识别 ----------
async def _recognize_async(mp3):
    shazam = Shazam()
    try:
        return await shazam.recognize(mp3)
    finally:
        try:
            await shazam.close()
        except Exception:
            pass


def shazam_recognize(mp3):
    for _ in range(3):
        try:
            res = asyncio.run(_recognize_async(mp3))
            track = res.get("track") if isinstance(res, dict) else None
            if track:
                title = track.get("title")
                artist = track.get("subtitle") or (
                    track.get("artists") and track["artists"][0].get("name"))
                if title:
                    return title, artist
        except Exception:
            time.sleep(2)
    return None, None


def mmss(sec):
    sec = int(round(sec))
    h_ = sec // 3600
    m_ = (sec % 3600) // 60
    s_ = sec % 60
    return f"{h_}:{m_:02d}:{s_:02d}" if h_ else f"{m_:02d}:{s_:02d}"


# ---------- 时间字符串解析 ----------
def parse_time(s):
    s = (s or "").strip()
    if not s:
        return None
    if ":" in s:
        parts = [float(p) for p in s.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return parts[0] * 60 + parts[1]
    if "分" in s or "秒" in s:
        mins = re.search(r"(\d+)\s*分", s)
        secs = re.search(r"(\d+)\s*秒", s)
        m_ = int(mins.group(1)) if mins else 0
        s_ = int(secs.group(1)) if secs else 0
        return m_ * 60 + s_
    try:
        return float(s)
    except ValueError:
        return None


# ---------- 主流程 ----------
def identify(bvid, start, end, use_vocal_removal=True, progress=None):
    def step(msg):
        if progress:
            progress(msg)

    step("正在连接 B 站并解析视频…")
    sign, h = wbi_sign()
    bvid = resolve_bvid(bvid, h)
    url, _, bw = get_audio_url(bvid, sign, h)

    # 只下载到区间终点（含余量），避免拉整段
    need_sec = end + 5
    needed = int(need_sec * bw / 8) + 300_000
    step("正在下载音频（仅区间所需部分）…")
    full = download_audio(url, h, needed)
    try:
        dur = get_duration(full) or 0
        if dur and end > dur:
            step(f"区间超出视频时长，已自动截断到 {mmss(dur)}")
            end = dur
        if start >= end:
            return {"title": None, "artist": None,
                    "used_vocal_removal": False,
                    "note": "起始时间须小于结束时间。"}

        workdir = tempfile.mkdtemp()
        # 切片用 wav：demucs 读取时走 soundfile 后端，避免依赖 ffmpeg/ffprobe
        clip = os.path.join(workdir, "clip.wav")
        step("正在截取区间音频…")
        if not extract_clip(full, start, end, clip):
            return {"title": None, "artist": None,
                    "used_vocal_removal": False,
                    "note": "音频截取失败，可能区间过短或该视频有版权限制。"}

        step("正在用 Shazam 识别…")
        title, artist = shazam_recognize(clip)
        used_vr = False
        note_vr = ""  # 默认值，避免去人声重试仍未命中时变量未定义

        if not title and use_vocal_removal:
            if demucs_available():
                step("直接识别未命中，正在去人声后重试…")
                nv = remove_vocals(clip, workdir)
                if nv:
                    used_vr = True
                    title, artist = shazam_recognize(nv)
            else:
                note_vr = "（已开启去人声，但服务端未安装 Demucs，跳过）"
        else:
            note_vr = ""

        # 清理临时文件
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

        if title:
            note = f"识别成功。区间 {mmss(start)}-{mmss(end)}"
            if used_vr:
                note += "（已自动去人声）"
        else:
            note = "未识别到匹配的 BGM。" + (note_vr if use_vocal_removal else "")
        return {"title": title, "artist": artist,
                "used_vocal_removal": used_vr, "note": note}
    finally:
        try:
            os.remove(full)
        except Exception:
            pass


if __name__ == "__main__":
    # 简单自测（需联网）
    import sys
    if len(sys.argv) >= 4:
        r = identify(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))
        print(json.dumps(r, ensure_ascii=False, indent=2))
