export const MANAGEMENT_PATH = "/manage";

type ManagementTab = "profiles" | "settings";

export function managementTabFromSearch(search: string): ManagementTab {
  const tab = new URLSearchParams(search).get("tab");
  return tab === "settings" ? "settings" : "profiles";
}

export function shouldScrollToBottomOnChatActivation(
  wasActive: boolean,
  isActive: boolean,
): boolean {
  return !wasActive && isActive;
}
