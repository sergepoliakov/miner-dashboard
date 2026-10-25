# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Stable display slots, explicit pauses, and unique requested-card enrollment."""
import fcntl,json,os,re,tempfile,time
from pathlib import Path
from runtime_paths import CONFIG_DIR
CONFIG=CONFIG_DIR/"gpu-display.json"
INVENTORY=CONFIG_DIR/"active-gpus.json"

def load_config(path=None):
    p=Path(path) if path else CONFIG
    d=json.loads(p.read_text()) if p.exists() else {"version":1,"slots":[]}
    if d.get("version")!=1 or not isinstance(d.get("slots"),list):raise ValueError("Invalid GPU display layout")
    numbers=[];uuids=[]
    for s in d["slots"]:
        if type(s.get("number")) is not int or s["number"]<1 or not isinstance(s.get("name"),str) or not s["name"]:raise ValueError("Invalid GPU display slot")
        numbers.append(s["number"])
        if s.get("uuid") is not None:
            if not re.fullmatch(r"GPU-[0-9a-fA-F-]{36}",s["uuid"]):raise ValueError("Invalid display UUID")
            uuids.append(s["uuid"])
    if len(set(numbers))!=len(numbers) or len(set(uuids))!=len(uuids):raise ValueError("Duplicate display number or UUID")
    return d

def title(s):
    return "GPU "+str(s["number"])+"（"+s["name"]+"）"

def paused_cards():
    d=json.loads(INVENTORY.read_text()) if INVENTORY.exists() else {}
    reasons=d.get("exclusion_reasons",{})
    return {u:reasons.get(u,"已暂停挖矿") for u in d.get("excluded_gpu_uuids",[])}

def hardware(card):
    try:
        domain,bus,slot=card["pci_bus_id"].lower().split(":")
        dev=Path("/sys/bus/pci/devices")/(f"{int(domain,16):04x}:{bus}:{slot}")
        ports=[v for v in dev.resolve().parts if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}[.][0-7]",v)]
        return {"subsystem_vendor":(dev/"subsystem_vendor").read_text().strip().lower(),
                "subsystem_device":(dev/"subsystem_device").read_text().strip().lower(),
                "link_width":int((dev/"current_link_width").read_text().strip()),
                "root_port":ports[0] if ports else None}
    except (OSError,ValueError,KeyError):return {}

def atomic_write(path,data):
    fd,tmp=tempfile.mkstemp(prefix=".gpu-display-",dir=path.parent)
    try:
        with os.fdopen(fd,"w") as f:json.dump(data,f,ensure_ascii=False,indent=2);f.write("\n");f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def auto_enroll(cards,path=None,hardware_probe=hardware):
    p=Path(path) if path else CONFIG
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with p.with_name(".gpu-display.lock").open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        d=load_config(p);known={s["uuid"] for s in d["slots"] if s.get("uuid")};changed=False
        for s in d["slots"]:
            rule=s.get("auto_bind")
            if s.get("uuid") or not isinstance(rule,dict) or rule.get("enabled") is not True:continue
            candidates=[]
            for c in cards:
                if c["uuid"] in known or not re.search(rule["name_regex"],c.get("name","")):continue
                hw=hardware_probe(c)
                if hw.get("subsystem_vendor")!=rule["subsystem_vendor"]:continue
                if "subsystem_device" in rule and hw.get("subsystem_device")!=rule["subsystem_device"]:continue
                if "link_width" in rule and hw.get("link_width")!=rule["link_width"]:continue
                candidates.append((c,hw))
            if len(candidates)!=1:continue
            c,hw=candidates[0];s["uuid"]=c["uuid"];known.add(c["uuid"]);changed=True
            s.pop("absent_state",None);s.pop("pending_note",None)
            identity=(s.get("known_identities") or {}).get(c["uuid"])
            if identity:
                s["name"]=identity["name"];s["model"]=identity["model"]
            s["binding_evidence"]={"source":"user_requested_unique_new_card",
                "uuid":c["uuid"],"reported_name":c.get("name"),"pci_bus_id":c.get("pci_bus_id"),
                "subsystem_vendor":hw["subsystem_vendor"],"subsystem_device":hw.get("subsystem_device"),"link_width":hw.get("link_width"),
                "root_port":hw.get("root_port"),"bound_at":time.time()}
        if changed:atomic_write(p,d)
    return d

def display_labels(cards,path=None):
    d=auto_enroll(cards,path)
    present={c["uuid"] for c in cards}
    return {s["uuid"]:title(s) for s in d["slots"] if s.get("uuid") in present}

def display_slots(gpus,path=None):
    d=load_config(path);live={g["uuid"]:g for g in gpus};out=[];used=set();paused=paused_cards()
    for s in sorted(d["slots"],key=lambda s:s["number"]):
        gpu=live.get(s.get("uuid"));used.add(s.get("uuid"))
        out.append({"display_number":s["number"],"display_name":s["name"],"display_title":title(s),"display_model":s.get("model",s["name"]),
            "uuid":s.get("uuid"),"present":gpu is not None,"pending":s.get("uuid") is None,
            "expected_slot":s.get("expected_slot"),"display_state":"在线" if gpu else s.get("absent_state","待安装" if not s.get("uuid") else "未接入"),
            "pending_note":s.get("pending_note") if not s.get("uuid") else None,"index":gpu.get("index",gpu.get("nvml_index")) if gpu else None,
            "mining_paused":s.get("uuid") in paused,"pause_reason":paused.get(s.get("uuid")),
            "auto_enroll":bool((s.get("auto_bind") or {}).get("enabled") and not s.get("uuid"))})
    number=max([s["display_number"] for s in out] or [0])
    for g in gpus:
        if g["uuid"] in used:continue
        number+=1;name=g.get("name","NVIDIA GPU").replace("NVIDIA GeForce ","")
        out.append({"display_number":number,"display_name":name,"display_title":f"GPU {number}（{name}）","display_model":name,
            "uuid":g["uuid"],"present":True,"pending":False,"expected_slot":None,"index":g.get("index",g.get("nvml_index")),
            "mining_paused":g["uuid"] in paused,"pause_reason":paused.get(g["uuid"]),"auto_enroll":False})
    return out

def decorate_gpus(gpus,path=None):
    slots={s["uuid"]:s for s in display_slots(gpus,path) if s["present"]}
    fields=["display_number","display_name","display_title","display_model","mining_paused","pause_reason"]
    return [{**g,**{k:slots[g["uuid"]][k] for k in fields}} for g in gpus]
