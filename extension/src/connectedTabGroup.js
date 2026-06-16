// 丹秘 tab group 管理：支持多 session 命名分组，每个 session 独立颜色。
// session="default" 保持旧 "丹秘" 灰色分组行为（向后兼容）。
// 依赖：manifest 必须声明 "tabGroups" 权限（外加已有的 "tabs"）。

const DEFAULT_GROUP_TITLE = '丹秘';
const DEFAULT_GROUP_COLOR = 'grey';

const COLORS = ['blue', 'cyan', 'purple', 'yellow', 'pink', 'red', 'orange', 'green'];
let colorIndex = 0;

// sessionName → {groupId: number, color: string, title: string, tabs: Set<number>}
const sessionGroups = new Map();

// --- chrome.tabs.onRemoved listener: auto-clean tabs from sessions ---

if (typeof chrome !== 'undefined' && chrome.tabs && chrome.tabs.onRemoved) {
  chrome.tabs.onRemoved.addListener((tabId) => {
    for (const [sessionName, session] of sessionGroups.entries()) {
      if (session.tabs.has(tabId)) {
        session.tabs.delete(tabId);
        if (session.tabs.size === 0) {
          sessionGroups.delete(sessionName);
        }
        break;
      }
    }
  });
}

// --- Internal helpers ---

function nextColor() {
  const color = COLORS[colorIndex % COLORS.length];
  colorIndex++;
  return color;
}

async function findDefaultGroup() {
  if (!chrome.tabGroups || !chrome.tabGroups.query) return null;
  try {
    const groups = await chrome.tabGroups.query({ title: DEFAULT_GROUP_TITLE });
    return groups && groups.length ? groups[0] : null;
  } catch (_) {
    return null;
  }
}

async function styleGroup(groupId, title, color) {
  try {
    await chrome.tabGroups.update(groupId, {
      title,
      color,
      collapsed: false,
    });
  } catch (_) {
    // group 可能在 race 中消失，忽略
  }
}

async function groupExists(groupId) {
  if (!chrome.tabGroups || !chrome.tabGroups.query) return false;
  try {
    const groups = await chrome.tabGroups.query({});
    return groups.some((g) => g.id === groupId);
  } catch (_) {
    return false;
  }
}

// --- Exported API ---

/**
 * 查找或创建 session 对应的 tab group。
 * sessionName="default" 使用旧的 "丹秘" 灰色 group 行为。
 */
export async function findOrCreateSessionGroup(sessionName, title) {
  if (!chrome.tabs || !chrome.tabs.group) return null;

  // Default session: 保持向后兼容
  if (sessionName === 'default') {
    const existing = await findDefaultGroup();
    if (existing) {
      // 确保 sessionGroups map 同步
      if (!sessionGroups.has('default')) {
        sessionGroups.set('default', {
          groupId: existing.id,
          color: DEFAULT_GROUP_COLOR,
          title: DEFAULT_GROUP_TITLE,
          tabs: new Set(),
        });
      } else {
        sessionGroups.get('default').groupId = existing.id;
      }
      return existing.id;
    }
    // default group 不存在时，返回 null；会在 addTabToSession 中创建
    return null;
  }

  // 已有 session：验证 group 仍然存在
  if (sessionGroups.has(sessionName)) {
    const session = sessionGroups.get(sessionName);
    const exists = await groupExists(session.groupId);
    if (exists) {
      return session.groupId;
    }
    // groupId 已失效，清除并重建
    sessionGroups.delete(sessionName);
  }

  // 新 session：暂时无法单独创建空 group，返回 null
  // 实际的 group 创建在 addTabToSession 中通过 chrome.tabs.group 完成
  return null;
}

/**
 * 把 tab 加入指定 session 的分组。
 * 如果 session 的 group 尚不存在，则创建并赋色。
 */
export async function addTabToSession(sessionName, tabId, title) {
  if (typeof tabId !== 'number') return null;
  if (!chrome.tabs || !chrome.tabs.group) return null;

  const displayTitle = sessionName === 'default'
    ? DEFAULT_GROUP_TITLE
    : (title || sessionName);
  const color = sessionName === 'default'
    ? DEFAULT_GROUP_COLOR
    : null; // 稍后决定

  let groupId = null;

  try {
    if (sessionGroups.has(sessionName)) {
      const session = sessionGroups.get(sessionName);
      const exists = await groupExists(session.groupId);
      if (exists) {
        groupId = await chrome.tabs.group({ tabIds: [tabId], groupId: session.groupId });
        session.tabs.add(tabId);
        return groupId;
      }
      // group 已失效，清除并重建
      sessionGroups.delete(sessionName);
    }

    // 尝试复用已有的 default group
    if (sessionName === 'default') {
      const existing = await findDefaultGroup();
      if (existing) {
        groupId = await chrome.tabs.group({ tabIds: [tabId], groupId: existing.id });
      } else {
        groupId = await chrome.tabs.group({ tabIds: [tabId] });
        await styleGroup(groupId, DEFAULT_GROUP_TITLE, DEFAULT_GROUP_COLOR);
      }
      if (!sessionGroups.has('default')) {
        sessionGroups.set('default', {
          groupId,
          color: DEFAULT_GROUP_COLOR,
          title: DEFAULT_GROUP_TITLE,
          tabs: new Set(),
        });
      } else {
        sessionGroups.get('default').groupId = groupId;
      }
      sessionGroups.get('default').tabs.add(tabId);
      return groupId;
    }

    // 非 default session：创建新 group
    const assignedColor = nextColor();
    groupId = await chrome.tabs.group({ tabIds: [tabId] });
    await styleGroup(groupId, displayTitle, assignedColor);

    sessionGroups.set(sessionName, {
      groupId,
      color: assignedColor,
      title: displayTitle,
      tabs: new Set([tabId]),
    });

    return groupId;
  } catch (err) {
    // tab 已关闭 / 无法分组：上层不依赖 group 成功，吞异常
    return null;
  }
}

/**
 * 从 session 中移除 tab（ungroup）。
 * 如果 session 的 tab 集合为空，则清除 session。
 */
export async function removeTabFromSession(sessionName, tabId) {
  if (typeof tabId !== 'number') return;
  if (!sessionGroups.has(sessionName)) return;

  const session = sessionGroups.get(sessionName);
  if (!session.tabs.has(tabId)) return;

  session.tabs.delete(tabId);

  try {
    if (chrome.tabs && chrome.tabs.ungroup) {
      await chrome.tabs.ungroup([tabId]);
    }
  } catch (_) {
    // tab 已关闭或不在 group 内
  }

  if (session.tabs.size === 0) {
    sessionGroups.delete(sessionName);
  }
}

/**
 * 关闭整个 session 的所有 tab 并清理。
 */
export async function closeSessionGroup(sessionName) {
  if (!sessionGroups.has(sessionName)) return { closed: 0 };

  const session = sessionGroups.get(sessionName);
  const tabIds = [...session.tabs].filter((id) => typeof id === 'number');

  sessionGroups.delete(sessionName);

  if (!tabIds.length) return { closed: 0 };

  try {
    await chrome.tabs.remove(tabIds);
  } catch (_) {
    // 部分 tab 可能已关闭
  }

  return { closed: tabIds.length };
}

/**
 * 获取 session 中所有 tab 的信息。
 */
export async function getSessionTabs(sessionName) {
  if (!sessionGroups.has(sessionName)) return [];

  const session = sessionGroups.get(sessionName);
  const results = [];

  for (const tabId of session.tabs) {
    try {
      const tab = await chrome.tabs.get(tabId);
      results.push({ tabId: tab.id, url: tab.url || '', title: tab.title || '' });
    } catch (_) {
      // tab 已关闭，从集合中移除
      session.tabs.delete(tabId);
    }
  }

  // 如果所有 tab 都已关闭，清理 session
  if (session.tabs.size === 0) {
    sessionGroups.delete(sessionName);
  }

  return results;
}

/**
 * 清理残留 group：移除 title 为 "丹秘" 的旧 group，
 * 并清理 sessionGroups 中 groupId 已不存在的条目。
 */
export async function cleanupStaleGroups() {
  if (!chrome.tabGroups || !chrome.tabGroups.query) return 0;

  let cleared = 0;

  // 清理旧的 "丹秘" 残留 group（兼容旧版行为）
  try {
    const groups = await chrome.tabGroups.query({ title: DEFAULT_GROUP_TITLE });
    if (groups && groups.length) {
      for (const g of groups) {
        try {
          const tabs = await chrome.tabs.query({ groupId: g.id });
          const ids = tabs.map((t) => t.id).filter((id) => typeof id === 'number');
          if (ids.length) {
            await chrome.tabs.ungroup(ids);
            cleared += ids.length;
          }
        } catch (_) {
          // 单个 group 清理失败不影响其它
        }
      }
    }
  } catch (_) {
    // query 失败，继续清理 map
  }

  // 清理 sessionGroups 中 groupId 已失效的条目
  for (const [sessionName, session] of sessionGroups.entries()) {
    const exists = await groupExists(session.groupId);
    if (!exists) {
      sessionGroups.delete(sessionName);
    }
  }

  return cleared;
}

// --- Backward-compatible aliases (CDP relay path still uses these) ---

export async function attachToGroup(tabId) {
  return addTabToSession('default', tabId);
}

export async function detachFromGroup(tabId) {
  return removeTabFromSession('default', tabId);
}
