/**
 * Simple structured logger for pipeline scripts
 */

const LOG_LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };
const level = (process.env.LOG_LEVEL || 'info').toLowerCase();
const currentLevel = LOG_LEVELS[level] ?? LOG_LEVELS.info;

function format(severity, message, data) {
  const entry = {
    timestamp: new Date().toISOString(),
    severity,
    message,
    ...(data && Object.keys(data).length > 0 && { data })
  };
  return JSON.stringify(entry);
}

export function debug(msg, data = {}) {
  if (currentLevel <= LOG_LEVELS.debug) {
    console.error(format('debug', msg, data));
  }
}

export function info(msg, data = {}) {
  if (currentLevel <= LOG_LEVELS.info) {
    console.error(format('info', msg, data));
  }
}

export function warn(msg, data = {}) {
  if (currentLevel <= LOG_LEVELS.warn) {
    console.error(format('warn', msg, data));
  }
}

export function error(msg, data = {}) {
  if (currentLevel <= LOG_LEVELS.error) {
    console.error(format('error', msg, data));
  }
}
