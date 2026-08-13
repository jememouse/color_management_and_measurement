/**
 * 后端接口封装。
 *
 * 所有写操作都会自动带上 X-Workbench 头 —— 服务端据此拒绝跨站请求。
 * 详见 server.py 的模块文档。
 */

const CSRF_HEADER = 'X-Workbench';

class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, { method = 'GET', body } = {}) {
  const options = {
    method,
    headers: { [CSRF_HEADER]: '1' },
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, options);
  } catch (cause) {
    // 网络层失败通常意味着服务已经停了, 给出比 "Failed to fetch" 更有用的话
    throw new ApiError('无法连接到本地服务 —— 请确认 server.py 仍在运行', 0, null);
  }

  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }

  if (!response.ok) {
    const message = (payload && payload.error) || `请求失败 (HTTP ${response.status})`;
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

export const api = {
  status: () => request('/api/status'),
  devices: () => request('/api/devices'),
  options: () => request('/api/options'),
  workflow: () => request('/api/workflow'),
  session: () => request('/api/session'),

  start: (action, params = {}) =>
    request('/api/session/start', { method: 'POST', body: { action, params } }),
  sendKey: (key) => request('/api/session/key', { method: 'POST', body: { key } }),
  sendText: (text) => request('/api/session/text', { method: 'POST', body: { text } }),
  stop: (force = false) => request('/api/session/stop', { method: 'POST', body: { force } }),

  files: () => request('/api/files'),
  deleteFile: (name) => request(`/api/files/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  downloadUrl: (name) => `/api/files/${encodeURIComponent(name)}`,
};

export { ApiError };

/**
 * 订阅服务端事件流。
 *
 * EventSource 断线后会自动重连, 且服务端每次都会先补一条 snapshot,
 * 所以刷新页面或短暂断网都不会丢失上下文。
 *
 * @param {Object<string, Function>} handlers 事件名 -> 回调
 * @returns {EventSource}
 */
export function connectStream(handlers) {
  const source = new EventSource('/api/session/stream');

  for (const [name, handler] of Object.entries(handlers)) {
    if (name === 'error' || name === 'open') continue;
    source.addEventListener(name, (event) => {
      let data = null;
      try {
        data = JSON.parse(event.data);
      } catch {
        data = event.data;
      }
      handler(data);
    });
  }

  if (handlers.open) source.addEventListener('open', handlers.open);
  if (handlers.error) source.addEventListener('error', handlers.error);

  return source;
}
