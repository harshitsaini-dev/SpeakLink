/**
 * Every page renders.
 *
 * WHY THIS EXISTS
 *
 * Receiver Status went completely white on a live estate: a component was
 * used and never imported, so the page threw a ReferenceError the moment it
 * mounted. Nothing caught it - the build succeeds, the unit tests exercised
 * other pages, and the failure only appears when somebody opens that one
 * screen.
 *
 * This mounts every page with a stubbed API and asserts only that something
 * rendered. It proves almost nothing about behaviour, and it would have
 * turned a white screen in front of the user into a red test in front of me.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";

jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  getToken: () => "t",
  wsUrl: (path) => `ws://localhost:8000${path}`,
  API_BASE: "http://localhost:8000/api",
  BACKEND_URL: "http://localhost:8000",
  isNetworkError: () => false,
}));
jest.mock("react-router-dom", () => ({
  Link: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
  NavLink: ({ to, children, ...rest }) => <a href={to} {...rest}>{children}</a>,
  Outlet: () => null,
  useNavigate: () => jest.fn(),
  useParams: () => ({ storeId: "1" }),
  useSearchParams: () => [new URLSearchParams(), jest.fn()],
}), { virtual: true });
jest.mock("recharts", () => {
  const Stub = ({ children }) => <div>{children}</div>;
  return new Proxy({}, { get: () => Stub });
});
jest.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { id: 1, username: "founder", role: "OWNER" },
    can: () => true, logout: jest.fn(),
  }),
}));
jest.mock("@/contexts/BroadcastContext", () => ({
  useBroadcast: () => ({ session: null, isLive: false, stores: [],
                         meta: { regions: [], cities: [] }, refresh: jest.fn() }),
}));
jest.mock("@/contexts/RecordingPlaybackContext", () => ({
  useRecordingPlayback: () => ({ active: null, playToken: jest.fn(),
                                 pauseToken: jest.fn(), stopPlayback: jest.fn() }),
}));

const { api } = require("@/lib/api");

const EMPTY_LIST = { items: [], total: 0, page: 1, pages: 0, has_more: false,
                     meta: {} };

beforeEach(() => {
  api.get.mockReset();
  // Everything answers with an empty list of the shape the admin pages expect.
  // A page that cannot survive "no data yet" is a page that breaks on the day
  // an estate is set up.
  api.get.mockResolvedValue({ data: EMPTY_LIST });
});

const PAGES = [
  ["Dashboard", "dashboard-page"],
  ["Announcements", "announcements-page"],
  ["AnnouncementTemplates", "templates-page"],
  ["AnnouncementRecordings", "recordings-page"],
  ["AnnouncementHistory", "announcement-history-page"],
  ["ReceiverStatus", "receivers-page"],
  ["StoreManagement", null],
  ["SystemLogs", null],
  ["UserManagement", null],
  ["BroadcastHistory", null],
  ["ReceiverDeviceFleet", null],
  ["ActiveBroadcasts", null],
];

test.each(PAGES)("%s mounts without throwing", async (name, testId) => {
  const Page = require(`./${name}`).default;
  const { container } = render(<Page />);

  if (testId) {
    await waitFor(() => expect(screen.getByTestId(testId)).toBeTruthy());
  } else {
    // No agreed testid on this page: mounting without throwing IS the
    // assertion, and an empty container would mean it rendered nothing.
    await waitFor(() => expect(container.firstChild).toBeTruthy());
  }
});
