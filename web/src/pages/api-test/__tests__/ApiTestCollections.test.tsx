import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen } from "@testing-library/react"
import { BrowserRouter } from "react-router-dom"
import ApiTestCollections from "../ApiTestCollections"

vi.mock("@/services/api", () => ({
  default: {
    get: vi.fn().mockResolvedValue({ code: 200, data: { data: [] } }),
    post: vi.fn().mockResolvedValue({ code: 200, data: { data: {} } }),
  },
}))

vi.mock("@/services/apiTestService", () => ({
  apiTestService: {
    getCollections: vi.fn().mockResolvedValue({ code: 200, data: [] }),
    getCases: vi.fn().mockResolvedValue({ code: 200, data: [] }),
    deleteCase: vi.fn().mockResolvedValue({ code: 200, message: "ok" }),
  },
}))

vi.mock("@/stores/projectStore", () => ({
  useProjectStore: () => ({ currentProjectId: 1 }),
}))

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))

const renderCollections = () =>
  render(
    <BrowserRouter>
      <ApiTestCollections />
    </BrowserRouter>
  )

describe("ApiTestCollections Page", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("should render without crashing", () => {
    renderCollections()
    expect(document.body).toBeTruthy()
  })

  // loadData 为异步：列头在首帧后随 fetch resolve 渲染，须用 findByText 等待
  it("should render case name column", async () => {
    renderCollections()
    expect(await screen.findByText("apiTest.caseName")).toBeTruthy()
  })

  it("should render request method column", async () => {
    renderCollections()
    expect(await screen.findByText("apiTest.requestMethod")).toBeTruthy()
  })

  it("should render request path column", async () => {
    renderCollections()
    expect(await screen.findByText("apiTest.requestPath")).toBeTruthy()
  })

  it("should render collection column", async () => {
    renderCollections()
    expect(await screen.findByText("apiTest.collection")).toBeTruthy()
  })

  it("should render updated at column", async () => {
    renderCollections()
    expect(await screen.findByText("common.updatedAt")).toBeTruthy()
  })
})
