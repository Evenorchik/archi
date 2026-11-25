# A clean Ubuntu with systemd as PID 1, to test install.sh the way it runs on a fresh server.
# docker build -t archi-test dev/test
# docker run -d --name archi-test --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw archi-test
FROM ubuntu:24.04
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && systemctl mask getty.target systemd-logind.service console-getty.service
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
