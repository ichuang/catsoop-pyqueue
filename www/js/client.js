/* Queue client for catsoop-pyqueue (port of imports/client.js).
 *
 * Same API as the original socket.io-based Client, but over a plain
 * WebSocket speaking the pyqueue JSON protocol:
 *
 *   client -> server   {event: name, data: {...}, id: 7}
 *   server -> client   {ack: 7, data: result}       (reply)
 *   server -> client   {event: name, data: {...}}   (push)
 */

class QueueClient {
    constructor(options) {
        options = options || {};
        if (options.socket_url) {
            this.url = options.socket_url;
        }
        else {
            const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            this.url = proto + '//' + window.location.host + (options.socket_path || '/ws');
        }
        this.room = options.room || 'default';
        this.username = null;

        this._next_id = 0;
        this._pending = {};      // id -> resolve
        this._listeners = {};    // event -> [callback]
        this._auth_msg = null;
        this._auth_callback = null;
        this._closed = false;

        this._connect();
    }

    _connect() {
        this.socket = new WebSocket(this.url);

        this.socket.onopen = () => {
            this._dispatch('connect', null);
            if (this._auth_msg) {
                // Re-authenticate automatically after a reconnect.
                this._emit('authenticate', this._auth_msg).then(auth => {
                    if (auth && auth.error) console.log('Auth error', auth.error);
                    else this.username = auth.username;
                    if (this._auth_callback) this._auth_callback(auth);
                });
            }
        };

        this.socket.onmessage = (event) => {
            let msg;
            try { msg = JSON.parse(event.data); }
            catch (err) { return; }

            if (msg.ack !== undefined) {
                const resolve = this._pending[msg.ack];
                delete this._pending[msg.ack];
                if (resolve) resolve(msg.data);
            }
            else if (msg.event) {
                this._dispatch(msg.event, msg.data);
            }
        };

        this.socket.onclose = () => {
            this._dispatch('disconnect', null);
            this._pending = {};
            if (!this._closed) {
                setTimeout(() => this._connect(), 1000);
            }
        };
    }

    _dispatch(name, data) {
        (this._listeners[name] || []).forEach(callback => callback(data));
    }

    _emit(name, msg) {
        return new Promise((resolve) => {
            const id = ++this._next_id;
            this._pending[id] = resolve;
            this.socket.send(JSON.stringify({event: name, data: msg || {}, id}));
        });
    }

    send(name, msg) {
        return this._emit(name, msg);
    }

    recv(name, callback) {
        (this._listeners[name] = this._listeners[name] || []).push(callback);
    }

    stop_recv(name, callback) {
        const listeners = this._listeners[name] || [];
        const idx = listeners.indexOf(callback);
        if (idx !== -1) listeners.splice(idx, 1);
    }

    close() {
        this._closed = true;
        this.socket.close();
    }

    login(auth_data, callback) {
        this._auth_msg = Object.assign({room: this.room}, auth_data);
        this._auth_callback = callback || (() => {});
        if (this.socket.readyState === WebSocket.OPEN) {
            this._emit('authenticate', this._auth_msg).then(auth => {
                if (auth && auth.error) console.log('Auth error', auth.error);
                else this.username = auth.username;
                this._auth_callback(auth);
            });
        }
        // otherwise onopen handles it
    }

    is_locked() { return this.send('get_locked'); }
    lock() { return this.send('lock'); }
    unlock() { return this.send('unlock'); }
    get_entries(filter) { return this.send('get_all', filter || {}); }
    add(type, data) { return this.send('add', {type, data}); }
    remove() { return this.send('action', {action: 'remove', username: this.username}); }
    clear() { return this.send('clear'); }
    action(type, data) { return this.send('action', Object.assign({action: type}, data)); }
    get_staff_list() { return this.send('get_staff_list'); }
    check_in(username) { return this.send('check_in', {username}); }
    check_out(username) { return this.send('check_out', {username}); }
}

if (typeof module !== 'undefined') module.exports = QueueClient;
