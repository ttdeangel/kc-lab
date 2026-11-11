#!/usr/bin/env bash
# ec2-bootstrap.sh - prepare an Amazon Linux 2023 or Ubuntu EC2 instance for kc-lab.sh
# Installs Docker, kind, kubectl, helm. Run once, then log out and back in.
set -euo pipefail

ARCH="$(uname -m)"; case "$ARCH" in x86_64) A=amd64;; aarch64) A=arm64;; *) echo "bad arch $ARCH"; exit 1;; esac

if command -v dnf >/dev/null; then
  sudo dnf install -y docker git
else
  sudo apt-get update && sudo apt-get install -y docker.io git curl
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

# kind
KIND_VER="$(curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | grep -m1 '"tag_name"' | cut -d'"' -f4)"
sudo curl -fsSLo /usr/local/bin/kind "https://kind.sigs.k8s.io/dl/${KIND_VER}/kind-linux-${A}"
sudo chmod +x /usr/local/bin/kind

# kubectl
KVER="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
sudo curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KVER}/bin/linux/${A}/kubectl"
sudo chmod +x /usr/local/bin/kubectl

# helm
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# kind needs higher inotify limits for many pods
echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
sudo sysctl --system >/dev/null

echo "Done. Log out and back in (docker group), then: HOG_REPLICAS=1 ./kc-lab.sh up"
