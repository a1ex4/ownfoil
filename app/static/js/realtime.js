/**
 * Client for the multiplexed realtime WebSocket at /api/ws.
 *
 * Generic: it owns the socket, the reconnect backoff and topic dispatch, and knows
 * nothing about what any topic carries.
 *
 *   const live = new Realtime(['tasks', 'workers']);
 *   live.on('tasks', (type, data) => { ... });   // type: snapshot|add|update|remove
 *   live.start();
 */
class Realtime {
    constructor(topics, {maxDelay = 10000} = {}) {
        this.topics = topics;
        this.maxDelay = maxDelay;
        this.handlers = {};
        this.statusHandlers = [];
        this.delay = 500;
        this.ws = null;
        this.stopped = false;
    }

    on(topic, handler) {
        (this.handlers[topic] = this.handlers[topic] || []).push(handler);
        return this;
    }

    /** Notified with true/false as the connection comes and goes. */
    onStatus(handler) {
        this.statusHandlers.push(handler);
        return this;
    }

    start() {
        this.stopped = false;
        this._connect();
        return this;
    }

    stop() {
        this.stopped = true;
        if (this.ws) this.ws.close();
    }

    _emitStatus(connected) {
        this.statusHandlers.forEach(h => h(connected));
    }

    _retry() {
        if (this.stopped) return;
        setTimeout(() => this._connect(), this.delay);
        this.delay = Math.min(this.delay * 2, this.maxDelay);
    }

    _connect() {
        const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
        const url = `${scheme}://${location.host}/api/ws?topics=${this.topics.join(',')}`;
        try {
            this.ws = new WebSocket(url);
        } catch (e) {
            // The constructor throws synchronously for a URL the browser refuses. Letting
            // that escape would kill the caller's script mid-setup and leave the page
            // frozen on its initial state with no handler ever attached to recover it.
            console.error('Realtime: could not open', url, e);
            this._emitStatus(false);
            this._retry();
            return;
        }

        this.ws.onopen = () => {
            this.delay = 500;  // a connection that worked earns a fast retry next time
            this._emitStatus(true);
        };

        this.ws.onmessage = (e) => {
            const event = JSON.parse(e.data);
            (this.handlers[event.topic] || []).forEach(h => h(event.type, event.data));
        };

        this.ws.onclose = () => {
            this._emitStatus(false);
            this._retry();
        };

        // onclose always follows onerror, so reconnection is handled in one place.
        this.ws.onerror = () => this.ws.close();
    }
}
