# -*- coding: utf-8 -*-
"""B 站视频 BGM 识别 —— Streamlit 网页版。"""
import streamlit as st
import bgm_core

st.set_page_config(
    page_title="B站视频 BGM 识别",
    page_icon="🎵",
    layout="centered",
)

st.title("🎵 B站视频 BGM 识别")
st.caption(
    "输入 B 站视频的 BV/AV 号与时间区间，自动识别这一段用的是什么背景音乐。"
    "区间内若被解说 / 对话盖住，会自动去人声后再识别。"
)

# 把识别结果存进 session_state，保证每次重新识别都会刷新，
# 且用户在输入框里改字时结果不会乱跳 / 残留旧值。
if "result" not in st.session_state:
    st.session_state.result = None
if "error" not in st.session_state:
    st.session_state.error = None

with st.form("bgm_form"):
    bvid = st.text_input(
        "视频 BV 号 / AV 号",
        placeholder="例如：BV16dJv6nEJH 或 AV123456",
    )
    c1, c2 = st.columns(2)
    with c1:
        start = st.text_input("起始时间", placeholder="1:30 / 90 / 1分30秒")
    with c2:
        end = st.text_input("结束时间", placeholder="2:05 / 125 / 2分05秒")
    use_vr = st.checkbox("被解说 / 对话盖住时自动去人声", value=True)
    submitted = st.form_submit_button(
        "开始识别", type="primary", use_container_width=True
    )


def run(bvid_val, s_sec, e_sec, use_vr):
    with st.status("准备中…", expanded=True) as status:
        def progress(msg):
            status.update(label=msg)

        try:
            res = bgm_core.identify(
                bvid_val, s_sec, e_sec,
                use_vocal_removal=use_vr, progress=progress,
            )
        except Exception as e:
            status.update(label="出错了 😢", state="error")
            st.session_state.error = str(e)
            st.session_state.result = None
            st.exception(e)
            return

        status.update(label="完成 ✅", state="complete")
        # 把结果写入 session_state，覆盖上一次的旧结果
        st.session_state.result = res
        st.session_state.query = (bvid_val, s_sec, e_sec)
        st.session_state.error = None


if submitted:
    if not bvid.strip():
        st.error("请填写视频 BV / AV 号")
    elif not start.strip() or not end.strip():
        st.error("请填写起始和结束时间")
    else:
        s_sec = bgm_core.parse_time(start)
        e_sec = bgm_core.parse_time(end)
        if s_sec is None or e_sec is None:
            st.error("时间格式无法识别，请参考示例（1:30 / 90 / 1分30秒）")
        elif s_sec < 0 or e_sec < 0:
            st.error("时间不能为负数，请参考示例（1:30 / 90 / 1分30秒）")
        elif e_sec <= s_sec:
            st.error("结束时间必须大于起始时间")
        else:
            run(bvid.strip(), s_sec, e_sec, use_vr)

# 统一在这里展示结果（始终从 session_state 读取），保证切换视频时能刷新
if st.session_state.error:
    st.divider()
    if st.button("清除报错信息"):
        st.session_state.error = None
        st.rerun()
elif st.session_state.result:
    res = st.session_state.result
    st.divider()
    if res.get("title"):
        st.success(res["note"])
        st.markdown("### 🎶 识别结果")
        st.markdown(f"**歌名：** {res['title']}")
        st.markdown(f"**歌手：** {res['artist'] or '未知'}")
        q = st.session_state.get("query")
        if q:
            st.caption(
                f"视频：{q[0]}　区间：{bgm_core.mmss(q[1])} - {bgm_core.mmss(q[2])}"
            )
    else:
        st.warning(res.get("note", "未识别到匹配的 BGM。"))

st.divider()
with st.expander("使用说明 / 时间格式"):
    st.markdown(
        """
        - **BV 号** 直接填 `BVxxxx`，**AV 号** 填 `AV123456` 或纯数字 `123456`。
        - **时间格式** 起止都支持：
          - `1:30` → 1 分 30 秒
          - `90` → 90 秒
          - `1分30秒` → 1 分 30 秒
        - 识别结果来自 Shazam，纯音乐 / 冷门 BGM 可能识别不到。
        - “自动去人声”依赖 Demucs（首次较慢），若服务端未安装会跳过该步骤。
        """
    )
