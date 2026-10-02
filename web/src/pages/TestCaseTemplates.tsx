/**
 * 用例模板库页面
 *
 * 系统内置模板（后端种子，只读）+ 用户自定义模板 CRUD，支持分类筛选与搜索。
 */
import { useCallback, useEffect, useState } from "react"
import { Card, Row, Col, Button, Tag, Typography, Space, Empty, message, Input, Modal, Form, Select, Popconfirm } from "antd"
import { CopyOutlined, SearchOutlined, PlusOutlined, DeleteOutlined, EditOutlined, DatabaseOutlined } from "@ant-design/icons"
import { useTranslation } from "react-i18next"
import { useSearchFilter } from "@/hooks/useSearchFilter"
import api, { type ApiResponse } from "@/services/api"

const { Text } = Typography
const { TextArea } = Input

interface Template {
  id: number
  user_id: number | null
  is_builtin: boolean
  name: string
  description: string
  category: string
  method: string
  endpoint: string
  headers: string
  body: string
  assertions: string
}

const METHOD_COLORS: Record<string, string> = {
  GET: "blue", POST: "green", PUT: "orange", DELETE: "red", PATCH: "purple",
}

const TestCaseTemplates: React.FC = () => {
  const { t } = useTranslation()
  const [templates, setTemplates] = useState<Template[]>([])
  const [loading, setLoading] = useState(false)
  const [categories, setCategories] = useState<string[]>([])
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<Template | null>(null)
  const [form] = Form.useForm()

  const fetchTemplates = useCallback(async () => {
    setLoading(true)
    try {
      const res = await (api.get("/test-case-templates") as unknown as ApiResponse)
      if (res.code === 200) setTemplates(res.data || [])
      const catRes = await (api.get("/test-case-templates/categories") as unknown as ApiResponse)
      if (catRes.code === 200) setCategories(catRes.data || [])
    } catch {
      message.error(t("common.failed"))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchTemplates() }, [fetchTemplates])

  const {
    searchText, setSearchText, filters, updateFilter, filteredData, hasActiveFilters, clearFilters,
  } = useSearchFilter<Template>({
    data: templates,
    searchFields: ['name', 'description'],
    filterFn: (item, f) => !f.category || item.category === f.category,
  })

  const handleUse = (tmpl: Template) => {
    navigator.clipboard.writeText(JSON.stringify(tmpl, null, 2))
    message.success(t("testTemplates.copied"))
  }

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ method: "GET", category: "通用", headers: "{}" })
    setModalOpen(true)
  }

  const openEdit = (tmpl: Template) => {
    setEditing(tmpl)
    form.setFieldsValue(tmpl)
    setModalOpen(true)
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      const res = editing
        ? await (api.put(`/test-case-templates/${editing.id}`, values) as unknown as ApiResponse)
        : await (api.post("/test-case-templates", values) as unknown as ApiResponse)
      if (res.code === 200) {
        message.success(editing ? t("testTemplates.updateSuccess") : t("testTemplates.createSuccess"))
        setModalOpen(false)
        fetchTemplates()
      } else {
        message.error(res.data?.message || t("common.failed"))
      }
    } catch {
      // 表单校验失败
    }
  }

  const handleDelete = async (id: number) => {
    const res = await (api.delete(`/test-case-templates/${id}`) as unknown as ApiResponse)
    if (res.code === 200) {
      message.success(t("testTemplates.deleteSuccess"))
      fetchTemplates()
    } else {
      message.error(res.data?.message || t("common.failed"))
    }
  }

  const card = (tmpl: Template) => (
    <Card size="small" hoverable style={{ marginBottom: 12 }}
      title={
        <Space size={6}>
          <Tag color={METHOD_COLORS[tmpl.method] || "default"}>{tmpl.method}</Tag>
          <Text strong>{tmpl.name}</Text>
          {tmpl.is_builtin && <Tag color="gold" icon={<DatabaseOutlined />}>{t("testTemplates.builtin")}</Tag>}
        </Space>
      }
      extra={
        <Space size={0}>
          <Button type="text" size="small" icon={<CopyOutlined />} onClick={() => handleUse(tmpl)} />
          {!tmpl.is_builtin && (
            <>
              <Button type="text" size="small" icon={<EditOutlined />} onClick={() => openEdit(tmpl)} />
              <Popconfirm title={t("testTemplates.deleteConfirm")} onConfirm={() => handleDelete(tmpl.id)}>
                <Button type="text" size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </>
          )}
        </Space>
      }
    >
      <Text type="secondary" style={{ fontSize: 12 }}>{tmpl.description}</Text>
      <div style={{ marginTop: 6, fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>{tmpl.endpoint}</div>
      {tmpl.assertions && (
        <div style={{ marginTop: 6, fontSize: 12 }}>
          <Text type="secondary">{t("testTemplates.assertions")}: </Text>
          <Text style={{ fontSize: 12 }}>{tmpl.assertions}</Text>
        </div>
      )}
      <div style={{ marginTop: 6 }}><Tag>{tmpl.category}</Tag></div>
    </Card>
  )

  return (
    <div className="fst-page">
      <div className="fst-page-header fst-animate-in">
        <h1 className="fst-page-title">{t("testTemplates.title")}</h1>
        <div className="fst-ios-card-subtitle">{t("testTemplates.subtitle")}</div>
      </div>
      <Card className="fst-ios-card fst-animate-in fst-animate-in-1">
        <Space style={{ marginBottom: 16 }} wrap>
          <Input prefix={<SearchOutlined />} placeholder={t("testTemplates.searchPlaceholder")} value={searchText} onChange={e => setSearchText(e.target.value)} style={{ width: 200 }} allowClear />
          <Tag color={filters.category === undefined ? undefined : "default"} style={{ cursor: "pointer", opacity: filters.category ? 0.5 : 1 }} onClick={() => updateFilter('category', undefined)}>全部</Tag>
          {categories.map(cat => (
            <Tag key={cat} color={filters.category === cat ? "blue" : undefined} style={{ cursor: "pointer" }} onClick={() => updateFilter('category', filters.category === cat ? undefined : cat)}>{cat}</Tag>
          ))}
          {hasActiveFilters && <Button size="small" type="link" onClick={clearFilters}>{t('common.clear') || '清除'}</Button>}
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>{t("testTemplates.createTemplate")}</Button>
        </Space>
        {loading ? (
          <Empty description={t("common.loading") || "加载中..."} />
        ) : filteredData.length === 0 ? (
          <Empty description={t("testTemplates.empty")} />
        ) : (
          <Row gutter={12}>
            {filteredData.map(tmpl => (
              <Col key={tmpl.id} xs={24} md={12} xl={8}>{card(tmpl)}</Col>
            ))}
          </Row>
        )}
      </Card>

      <Modal
        title={editing ? t("testTemplates.editTemplate") : t("testTemplates.createTemplate")}
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={handleSave}
        okText={t("common.save") || "保存"}
        cancelText={t("common.cancel") || "取消"}
        width={640}
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label={t("testTemplates.name")} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("testTemplates.description")}>
            <Input />
          </Form.Item>
          <Space size={12} style={{ display: "flex" }}>
            <Form.Item name="method" label={t("apiTest.method")} style={{ width: 110 }}>
              <Select options={["GET", "POST", "PUT", "DELETE", "PATCH"].map(m => ({ value: m, label: m }))} />
            </Form.Item>
            <Form.Item name="category" label={t("testTemplates.category")} style={{ width: 160 }}>
              <Input />
            </Form.Item>
          </Space>
          <Form.Item name="endpoint" label={t("testTemplates.endpoint")}>
            <Input placeholder="{{base_url}}/api/resource" style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item name="headers" label={t("perfTest.requestHeaders")}>
            <TextArea rows={2} placeholder='{"Content-Type": "application/json"}' style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item name="body" label={t("perfTest.requestBody")}>
            <TextArea rows={3} placeholder='{"name": "test"}' style={{ fontFamily: "monospace" }} />
          </Form.Item>
          <Form.Item name="assertions" label={t("testTemplates.assertions")}>
            <Input placeholder="status=200, body.data.id exists" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  )
}

export default TestCaseTemplates
