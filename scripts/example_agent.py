#!/usr/bin/env python3
"""示例 Agent：演示 generic 后端如何把任意 CLI 接入微信通道。

用法（与 backend.json 里 generic.new_cmd / resume_cmd 对应）：
  python example_agent.py -p "提示词"                 # 新会话
  python example_agent.py -p "提示词" -s <session_id> # 续接会话

真实 Agent 只要做到：把回答打到 stdout，把会话 ID 打到 stdout/stderr 的
session= 里，并可选输出【计划】【进度】【总结】【文件】标记行即可。
"""
import argparse
import time
import uuid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--prompt", required=True)
    ap.add_argument("-s", "--session", default=None)
    args = ap.parse_args()
    sid = args.session or str(uuid.uuid4())
    print("【计划】1. 解析输入 2. 生成回答 3. 输出结果")
    print(f"【进度】第1步完成：收到消息「{args.prompt[:20]}」")
    time.sleep(0.1)
    print("【进度】第2步完成：已生成回答")
    print("【进度】第3步完成：已输出结果")
    print(f"【总结】这是示例 Agent 的回答（会话 {sid[:8]}）")
    print(f"session={sid}")


if __name__ == "__main__":
    main()
