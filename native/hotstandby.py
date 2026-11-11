"""One retained SoftEther spare, isolated from the active tunnel's lifecycle."""
import ipaddress
import threading
import time


class HotStandby:
    def __init__(self, manager, backend):
        self.m = manager
        self.backend = backend
        self.lock = threading.RLock()
        self.slot = None
        self.cancel = threading.Event()
        self.cooldown = {}
        self.building_node = None

    def status(self, cfg):
        # UI reads a snapshot without taking the standby lock: callers may
        # already hold the manager lock used by the worker's state writes.
        slot = self.slot
        node = slot['node'] if slot else self.building_node
        disabled = not cfg.get('connection_enabled', True) or cfg.get('routing_mode') == 'fixed_ip'
        age = max(0, int(time.monotonic()-slot['verified'])) if slot else None
        ready = bool(slot and not disabled and time.monotonic()-slot['verified'] <= 5)
        status = 'disabled' if disabled else ('ready' if ready else ('checking' if slot else ('building' if node else 'waiting')))
        fields = ('id', 'ip', 'remote_host', 'remote_port', 'country', 'country_short', 'location', 'owner', 'as_name', 'ip_type')
        return dict(standby_status=status, standby_ready=ready,
                    standby_node_id=node['id'] if node and not disabled else '',
                    standby_node={k: node.get(k) for k in fields} if node and not disabled else None,
                    standby_latency_ms=slot.get('latency_ms') if slot and not disabled else None,
                    standby_verified_seconds=age if not disabled else None)

    def dispose(self, slot):
        if slot:
            try:
                slot['session'].terminate()
            finally:
                self.m.release_test_index(slot['index'])

    def clear(self):
        with self.lock:
            self.cancel.set()
            slot, self.slot = self.slot, None
        self.dispose(slot)

    def verify(self, session, timeout=1.5, url='https://api.ipify.org'):
        started = time.monotonic()
        result = self.backend.command(
            'curl', '-4', '-fsS', '--proxy', '', '--noproxy', '*',
            '--interface', session.interface, '--max-time', str(timeout),
            url, check=False,
        )
        if result.returncode:
            return None
        try:
            ipaddress.ip_address(result.stdout.strip())
        except ValueError:
            return None
        return {'ip': result.stdout.strip(), 'latency_ms': max(1, int((time.monotonic()-started)*1000))}

    def allowed(self, node, cfg):
        if not cfg.get('connection_enabled', True) or cfg.get('routing_mode') == 'fixed_ip':
            return False
        return bool(self.m.apply_routing_filters([node], cfg)) and node['id'] != self.m.active_openvpn_node_id

    def tick(self):
        cfg = self.m.load_ui_config()
        if not cfg.get('connection_enabled', True) or cfg.get('routing_mode') == 'fixed_ip':
            self.clear()
            self.m.set_state(standby_node_id='', standby_ready=False)
            return
        with self.lock:
            slot = self.slot
        if slot:
            current = next((n for n in self.m.read_nodes() if n['id'] == slot['node']['id']), None)
            result = self.verify(slot['session']) if current and self.allowed(current, cfg) else None
            if (result is None and slot.get('failures', 0) >= 1
                    and current and self.allowed(current, cfg)):
                result = self.verify(slot['session'], timeout=3, url='https://ip.sb')
            with self.lock:
                if self.slot is not slot:
                    return
                if result:
                    slot.update(result, verified=time.monotonic(), failures=0)
                    self.m.set_state(standby_node_id=current['id'], standby_ready=True)
                    return
                slot['failures'] = slot.get('failures', 0)+1
                if slot['failures'] < 2 and current and self.allowed(current, cfg):
                    return
                self.slot = None
            self.cooldown[slot['node']['id']] = time.monotonic()+60
            self.dispose(slot)
            self.m.set_state(standby_node_id='', standby_ready=False)
        if not self.m.active_openvpn_running() or self.m.is_connecting:
            return
        blacklist = self.m.load_blacklist()
        nodes = self.m.read_nodes()
        candidates = [n for n in nodes if n.get('probe_status') == 'available'
                      and n['id'] not in blacklist and self.allowed(n, cfg)
                      and self.cooldown.get(n['id'], 0) < time.monotonic()]
        reference = next((n for n in nodes if n['id'] == self.m.active_openvpn_node_id), None)
        candidates.sort(key=lambda n: self.m.backup_priority_key(n, reference))
        if not candidates:
            self.m.set_state(standby_node_id='', standby_ready=False)
            return
        node = candidates[0]
        index = self.m.get_free_test_index()
        self.building_node = node
        with self.lock:
            self.cancel = threading.Event()
            cancel = self.cancel
        path = self.m.test_config_path(node['id'])
        session = None
        try:
            path.write_text(node['config_text'], encoding='utf-8')
            ok, message, session = self.backend.run_until_ready(
                str(path), False, self.m.NODE_EXIT_PROBE_TIMEOUT_SECONDS,
                f'tun{index}', cancel_event=cancel, retain=True,
            )
            if not ok or not session:
                self.cooldown[node['id']] = time.monotonic()+60
                return
            result = self.verify(session)
            with self.lock:
                if cancel.is_set() or not result or not self.allowed(node, self.m.load_ui_config()):
                    return
                self.slot = dict(node=node, session=session, index=index, verified=time.monotonic(), **result)
                session = None
            self.m.set_state(standby_node_id=node['id'], standby_ready=True)
            self.m.log_to_json('INFO', 'VPN', f"热备就绪: {node['id']}")
        finally:
            self.building_node = None
            path.unlink(missing_ok=True)
            with self.lock:
                retained = self.slot is not None and self.slot['index'] == index
            if not retained:
                try:
                    if session:
                        session.terminate()
                finally:
                    self.m.release_test_index(index)

    def promote(self, reason="主节点出口异常，提升热备"):
        with self.lock:
            slot = self.slot
            if not slot or time.monotonic()-slot['verified'] > 5:
                return False
        token, cancel = self.m.begin_connection_attempt()
        started = time.monotonic()
        previous_state = self.m.get_state()
        from_ip = previous_state.get('last_verified_exit_ip') or previous_state.get('proxy_ip')
        from_node = self.m.active_openvpn_node_id or self.m.load_ui_config().get('last_native_node_id', '')
        old = None
        try:
            cfg = self.m.load_ui_config()
            current = next((n for n in self.m.read_nodes() if n['id'] == slot['node']['id']), None)
            with self.lock:
                if self.slot is not slot or not current or not self.allowed(current, cfg):
                    return False
                if not self.m.connection_attempt_is_current(token, cancel):
                    return False
                self.slot = None
            original = slot['session'].terminate
            release_lock = threading.Lock()
            released = False
            def terminate():
                nonlocal released
                with release_lock:
                    if released:
                        return
                    try:
                        original()
                    finally:
                        released = True
                        self.m.release_test_index(slot['index'])
            slot['session'].terminate = terminate
            with self.m.lock:
                cfg = self.m.load_ui_config()
                if not self.m.connection_attempt_is_current(token, cancel) or not self.allowed(current, cfg):
                    slot['session'].terminate()
                    return False
                old = self.m.active_openvpn_process
                self.m.active_openvpn_process = slot['session']
                self.m.active_openvpn_node_id = current['id']
                self.m.proxy_server.set_outbound_interface(slot['session'].interface)
                nodes = self.m.read_nodes()
                for node in nodes:
                    node['active'] = node['id'] == current['id']
                    if node['active']:
                        node.update(probe_status='available', exit_verified_at=time.time())
                self.m.write_json(self.m.NODES_FILE, nodes)
                cfg['last_native_node_id'] = current['id']
                self.m.write_json(self.m.DATA_DIR / 'ui_auth.json', cfg)
                self.m.reset_proxy_failure_counter(current['id'])
                self.m.set_state(active_openvpn_node_id=current['id'], proxy_ok=True,
                                 proxy_ready=True, tunnel_ready=True, proxy_ip=slot['ip'],
                                 proxy_latency_ms=slot['latency_ms'], proxy_error='',
                                 standby_ready=False, standby_node_id='',
                                 last_check_message=f"已提升热备节点: {current['id']}")
            self.m.log_to_json('INFO', 'VPN', f"热备提升成功: {current['id']}")
            self.m.record_switch_event(from_node, current['id'], 'auto', 'success', reason, started, from_exit_ip=from_ip, to_exit_ip=slot['ip'])
            return True
        finally:
            self.m.finish_connection_attempt(token, cancel)
            if old:
                # Existing failed sockets cannot migrate; new requests already
                # use the spare while the old DHCP/account is being removed.
                threading.Thread(target=self.m.stop_process, args=(old,), daemon=True).start()

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as exc:
                self.m.log_to_json('WARNING', 'VPN', f'热备维护失败: {exc}')
            time.sleep(2)
