%global debug_package %{nil}
%global _build_id_links none

Name:           zeroclaw
Version:        0.6.5
Release:        1
Summary:        ZeroClaw daemon runtime
License:        MIT OR Apache-2.0
AutoReqProv:    no

Source0:        zeroclawlabs
Source1:        zeroclaw.service

%description
ZeroClaw Tizen runtime package. Installs the daemon binary, root-managed
systemd unit. The runtime state under
/root/.zeroclaw is intentionally not packaged here.

%prep

%build

%install
install -Dpm0755 %{SOURCE0} %{buildroot}/usr/bin/zeroclaw
install -Dpm0644 %{SOURCE1} %{buildroot}/usr/lib/systemd/system/zeroclaw.service

%pre
if [ "$1" -gt 1 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl stop zeroclaw.service >/dev/null 2>&1 || true
fi

%post
if command -v install >/dev/null 2>&1; then
    if [ ! -d /root/.zeroclaw ]; then
        install -d -m 0755 /root/.zeroclaw >/dev/null 2>&1 || true
    fi
    if [ ! -d /root/.zeroclaw/workspace ]; then
        install -d -m 0755 /root/.zeroclaw/workspace >/dev/null 2>&1 || true
    fi
fi
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ -f /root/.zeroclaw/config.toml ]; then
        systemctl enable zeroclaw.service >/dev/null 2>&1 || true
        if systemctl restart zeroclaw.service >/dev/null 2>&1; then
            zeroclaw_active=0
            for _ in 1 2 3 4 5; do
                if systemctl is-active --quiet zeroclaw.service; then
                    zeroclaw_active=1
                    break
                fi
                sleep 1
            done
            if [ "$zeroclaw_active" -eq 1 ]; then
                echo "zeroclaw.service restarted and is active"
            else
                echo "warning: zeroclaw.service did not become active within 5 seconds after install"
                echo "check status: systemctl status zeroclaw.service --no-pager -l"
                echo "check journal: journalctl -u zeroclaw.service -n 100 --no-pager"
            fi
        else
            echo "warning: systemctl restart zeroclaw.service failed during install"
            echo "check status: systemctl status zeroclaw.service --no-pager -l"
            echo "check journal: journalctl -u zeroclaw.service -n 100 --no-pager"
        fi
    else
        echo "notice: /root/.zeroclaw/config.toml is absent; zeroclaw.service was installed but not started"
        echo "next step: install the ZeroClaw state package or place config under /root/.zeroclaw, then run: systemctl enable --now zeroclaw.service"
    fi
fi

%preun
if [ "$1" -eq 0 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl stop zeroclaw.service >/dev/null 2>&1 || true
    systemctl disable zeroclaw.service >/dev/null 2>&1 || true
fi

%postun
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi

%files
%attr(0755,root,root) /usr/bin/zeroclaw
%attr(0644,root,root) /usr/lib/systemd/system/zeroclaw.service
