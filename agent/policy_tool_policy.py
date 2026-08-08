"""Mandatory pre-dispatch policy for the untrusted local fallback worker."""
import ipaddress, json, re, socket
from urllib.parse import urlsplit

PROXMOX_MARKERS=(":8006","/api2/json","/api2/extjs","/pve2/","pveapitoken","pveauthcookie","proxmox","porx"," pvesh "," pvecm "," pvesm "," qm "," pct ")
NETWORK_CHANGE=("iptables","nft ","ip route","ip link","nmcli","resolv.conf","wg-quick")
SYSTEM_DELETE=("rm -rf /","rm -r /etc","rm -r /var","wipefs","mkfs")
SSH=(" ssh ","scp ","sftp ","rsync ")
URL_RE=re.compile(r"https?://[^\s'\"`<>]+",re.I)

def _strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,dict):
        for item in value.values():yield from _strings(item)
    elif isinstance(value,(list,tuple)):
        for item in value:yield from _strings(item)

def _blocked_url(url):
    try:
        p=urlsplit(url); host=(p.hostname or "").casefold().rstrip(".")
        if p.port==8006 or any(x in p.path.casefold() for x in ("/api2/json","/api2/extjs","/pve2/")):return True
        if host in {"porx","porx.local","proxmox","pve"} or "proxmox" in host:return True
        for info in socket.getaddrinfo(host,p.port or 443,type=socket.SOCK_STREAM):
            ip=ipaddress.ip_address(info[4][0])
            if ip in ipaddress.ip_network("10.50.50.0/24"):return True
    except Exception:
        # A malformed/suspicious Proxmox-looking target fails closed; ordinary
        # unresolved public URLs are left for the tool's own error handling.
        return any(x in url.casefold() for x in PROXMOX_MARKERS)
    return False

def enforce(function_name,args):
    try:
        from agent.policy_fallback import runtime
        if runtime() is None:return None
    except Exception:return None
    text=" "+" ".join(_strings(args)).casefold()+" "
    if any(x in text for x in PROXMOX_MARKERS):return "Denied: policy fallback cannot access Proxmox or actions outside its VM"
    for url in URL_RE.findall(text):
        if _blocked_url(url):return "Denied: resolved/final network target is Proxmox management infrastructure"
    if any(x in text for x in SSH+NETWORK_CHANGE+SYSTEM_DELETE):
        from tools.approval import request_tool_approval
        decision = request_tool_approval(
            function_name,
            "Qwen policy-fallback requests an infrastructure-sensitive action",
            rule_key="policy_fallback_sensitive_action",
        )
        if not decision.get("approved"):
            return decision.get("message") or "Denied: required human confirmation was not granted"
    return None
