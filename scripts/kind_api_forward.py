"""把 kind 节点里的 kube-apiserver 暴露到宿主机（当集群容器**没有发布端口**时用）。

背景：`kind create cluster` 正常会给节点容器发布 6443 → 宿主机随机端口，`~/.kube/config`
也按那个端口写。但如果容器是后来重建/恢复的，端口映射可能**不在了**（`docker port <node>`
输出为空），于是 `kubectl` 报：

    dial tcp 127.0.0.1:58583: connectex: No connection could be made because the
    target machine actively refused it.

本脚本用「桥接网络里的转发容器 + 发布端口」把这条链路接回来：

    NODE_IP=$(docker inspect -f \
        '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' warden-control-plane)
    docker run -d --name warden-api-forward --restart unless-stopped \
        -p 58583:58583 -e UPSTREAM=$NODE_IP:6443 \
        -v "$PWD/scripts/kind_api_forward.py:/fwd.py:ro" python:3.12-slim python /fwd.py

它在**默认桥接网络**上监听 58583，把流量转给节点 IP 的 6443。宿主机 `~/.kube/config` 里的
`https://127.0.0.1:58583` 于是可用。

为什么不能直接共享节点的网络命名空间：docker 不允许「端口发布 + container 网络模式」同时用
（`conflicting options: port publishing and the container type network mode`）；而且那样也不能
监听 6443——那个端口已被 kube-apiserver 占用。

退出码契约（把「哪一层的事实」说清楚，别让值班的人查错方向）：
    0  = 正常（转发中，阻塞不返回）
    1  = 真失败（绑定出错但不是被占、上游配置不可用……）
    3  = **前置条件已被占**：`LISTEN_PORT` 上已经有东西在应答。这时容器会立刻退出，
        而 docker 只会报一句「创建转发容器失败」——那句话是**这一层**的，事实是**上一层**的
        （常见原因：上一次的 `warden-api-forward` 还活着，或别的东西占了这个端口）。
        先问一次再绑，就是为了把这句话说对。

TLS 是端到端校验的：kubectl 看到的服务端名字仍是 `127.0.0.1`，kind 生成的 apiserver 证书
包含这个 SAN，所以中间多一跳不影响校验。

用法（宿主机）：
    NODE_IP=$(docker inspect -f \
        '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' warden-control-plane)
    docker run -d --name warden-api-forward --restart unless-stopped \
        -p 58583:58583 -e UPSTREAM=$NODE_IP:6443 \
        -v "$PWD/scripts/kind_api_forward.py:/fwd.py:ro" python:3.12-slim python /fwd.py
    kubectl get nodes                    # 应当直接通了

注意：证书里必须有 127.0.0.1 —— kind 生成的 apiserver 证书默认包含它，所以 TLS 校验能过。
"""

import contextlib
import errno
import os
import socket
import sys
import threading

LISTEN = ("0.0.0.0", int(os.environ.get("LISTEN_PORT", "58583")))
# 节点的 docker 内网 IP。取法：
#     docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' <节点名>
# 默认 127.0.0.1 只适用于「与节点共享网络命名空间」的跑法，而 docker 不允许那种跑法再发布端口。
_UP = os.environ.get("UPSTREAM", "127.0.0.1:6443")
UPSTREAM = (_UP.rsplit(":", 1)[0], int(_UP.rsplit(":", 1)[1]))
BUF = 65536


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        with contextlib.suppress(OSError):
            while True:
                chunk = src.recv(BUF)
                if not chunk:
                    break
                dst.sendall(chunk)
    finally:
        for sock in (src, dst):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PORT_BUSY = 3
PROBE_ADDR = ("127.0.0.1", LISTEN[1])


def port_has_listener(addr: tuple[str, int], timeout: float = 3.0) -> bool:
    """端口上是否已经有东西在应答。

    先问再绑：`bind()` 失败时，「端口被占」与「本脚本坏了」是完全不同的两件事，
    而 docker 只会把两者都报成「创建转发容器失败」。
    """
    try:
        with socket.create_connection(addr, timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    if port_has_listener(PROBE_ADDR):
        print(
            f"{PROBE_ADDR[0]}:{PROBE_ADDR[1]} 上已经有东西在应答 —— 端口已被占用。\n"
            "常见原因：上一次的转发容器还活着。确认："
            f"docker ps --filter name=warden-api-forward\n"
            f"（本进程不做任何改动就退出，退出码 {EXIT_PORT_BUSY}）",
            flush=True,
        )
        return EXIT_PORT_BUSY
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(LISTEN)
    except OSError as exc:
        # 竞态：上一句探测时还没人占，绑之前被抢走了——也要说清是哪一层的事实。
        print(f"绑定 {LISTEN[0]}:{LISTEN[1]} 失败：{exc}", flush=True)
        return EXIT_PORT_BUSY if exc.errno == errno.EADDRINUSE else EXIT_FAILED
    server.listen(64)
    print(f"forwarding {LISTEN[0]}:{LISTEN[1]} -> {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
    while True:
        client, _ = server.accept()
        try:
            upstream = socket.create_connection(UPSTREAM, timeout=10)
        except OSError as exc:  # 上游不可达时不要静默吞掉连接
            print(f"upstream {UPSTREAM} unreachable: {exc}", flush=True)
            client.close()
            continue
        upstream.settimeout(None)
        threading.Thread(target=_pump, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pump, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
