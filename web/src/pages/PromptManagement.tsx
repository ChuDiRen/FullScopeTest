import { useCallback, useEffect, useState } from 'react'
import { Button, Card, Form, Input, InputNumber, message, Modal, Popconfirm, Row, Col, Select, Space, Switch, Table, Tag, Tooltip, Badge } from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, CheckCircleOutlined, ReloadOutlined, ExperimentOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import {
  promptVersionService, PromptVersion, PromptVersionPayload, PROMPT_FEATURES,
} from '../services/promptVersionService'

/**
 * Prompt 版本管理页 —— 自系统设置迁入「AI 助手」分组（/ai-assistant/prompts）。
 * 管理各 AI 功能的 system prompt 多版本、A/B 流量权重与效果统计。
 */
const PromptManagement = () => {
  const { t } = useTranslation()
  const [versions, setVersions] = useState<PromptVersion[]>([])
  const [loading, setLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [filterFeature, setFilterFeature] = useState<string | undefined>()
  const [modalOpen, setModalOpen] = useState(false)
  const [editingVersion, setEditingVersion] = useState<PromptVersion | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()

  const loadVersions = useCallback(async (p = page, feature = filterFeature) => {
    setLoading(true)
    try {
      const res = await promptVersionService.list({ page: p, per_page: 10, feature })
      if (res.code === 200 && res.data) {
        setVersions(res.data.items || [])
        setTotal(res.data.total || 0)
      }
    } catch {
      message.error(t('common.failed') || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [page, filterFeature, t])

  useEffect(() => { loadVersions(1); }, [filterFeature])

  const handleCreate = () => {
    setEditingVersion(null)
    form.resetFields()
    form.setFieldsValue({ temperature: 0.3, traffic_weight: 1.0, is_active: false })
    setModalOpen(true)
  }

  const handleEdit = (record: PromptVersion) => {
    setEditingVersion(record)
    form.setFieldsValue({
      feature: record.feature,
      name: record.name,
      system_prompt: record.system_prompt,
      user_prompt_template: record.user_prompt_template || '',
      temperature: record.temperature,
      model_name: record.model_name || '',
      is_active: record.is_active,
      traffic_weight: record.traffic_weight,
      change_notes: record.change_notes || '',
    })
    setModalOpen(true)
  }

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const payload: PromptVersionPayload = {
        feature: values.feature,
        name: values.name,
        system_prompt: values.system_prompt,
        user_prompt_template: values.user_prompt_template || undefined,
        temperature: values.temperature,
        model_name: values.model_name || undefined,
        is_active: values.is_active,
        traffic_weight: values.traffic_weight,
        change_notes: values.change_notes || undefined,
      }
      if (editingVersion) {
        const res = await promptVersionService.update(editingVersion.id, payload)
        if (res.code === 200) {
          message.success(t('prompt.updated') || '版本更新成功')
          setModalOpen(false)
          loadVersions()
        } else {
          message.error(res.message)
        }
      } else {
        const res = await promptVersionService.create(payload)
        if (res.code === 200) {
          message.success(t('prompt.created') || '版本创建成功')
          setModalOpen(false)
          loadVersions()
        } else {
          message.error(res.message)
        }
      }
    } catch {
      // 表单校验失败不处理
    } finally {
      setSaving(false)
    }
  }

  const handleDeactivate = async (id: number) => {
    try {
      const res = await promptVersionService.deactivate(id)
      if (res.code === 200) {
        message.success(t('prompt.deactivated') || '版本已停用')
        loadVersions()
      }
    } catch {
      message.error(t('common.failed') || '操作失败')
    }
  }

  const handleActivate = async (feature: string) => {
    try {
      const res = await promptVersionService.select(feature)
      if (res.code === 200) {
        message.success(t('prompt.activated') || '版本已激活')
        loadVersions()
      }
    } catch {
      message.error(t('common.failed') || '操作失败')
    }
  }

  const handleRefreshStats = async () => {
    try {
      const res = await promptVersionService.refreshStats(filterFeature)
      if (res.code === 200) {
        message.success(t('prompt.statsRefreshed') || '统计数据已刷新')
        loadVersions()
      }
    } catch {
      message.error(t('common.failed') || '刷新失败')
    }
  }

  const featureOptions = [
    { label: t('prompt.allFeatures') || '全部功能', value: '' },
    ...PROMPT_FEATURES.map(f => ({ label: f.label, value: f.value })),
  ]

  const columns = [
    {
      title: t('prompt.feature') || '功能',
      dataIndex: 'feature',
      key: 'feature',
      width: 120,
      render: (val: string) => {
        const found = PROMPT_FEATURES.find(f => f.value === val)
        return <Tag color="blue">{found?.label || val}</Tag>
      },
    },
    {
      title: t('prompt.name') || '名称',
      dataIndex: 'name',
      key: 'name',
      width: 160,
      render: (val: string, record: PromptVersion) => (
        <Space size={4}>
          <span style={{ fontWeight: 500 }}>{val}</span>
          <span style={{ color: 'var(--fst-on-surface-muted, #999)', fontSize: 12 }}>v{record.version}</span>
        </Space>
      ),
    },
    {
      title: t('prompt.status') || '状态',
      dataIndex: 'is_active',
      key: 'is_active',
      width: 80,
      render: (val: boolean) => val
        ? <Badge status="success" text={t('prompt.active') || '激活'} />
        : <Badge status="default" text={t('prompt.inactive') || '停用'} />,
    },
    {
      title: t('prompt.temperature') || '温度',
      dataIndex: 'temperature',
      key: 'temperature',
      width: 70,
      render: (val: number) => val?.toFixed(1),
    },
    {
      title: t('prompt.trafficWeight') || '流量权重',
      dataIndex: 'traffic_weight',
      key: 'traffic_weight',
      width: 90,
      render: (val: number) => `${(val * 100).toFixed(0)}%`,
    },
    {
      title: t('prompt.invocations') || '调用次数',
      dataIndex: 'total_invocations',
      key: 'total_invocations',
      width: 90,
      render: (val: number) => val?.toLocaleString() || '0',
    },
    {
      title: t('prompt.successRate') || '成功率',
      dataIndex: 'success_rate',
      key: 'success_rate',
      width: 80,
      render: (val: number) => {
        const color = val >= 90 ? '#52c41a' : val >= 70 ? '#faad14' : '#ff4d4f';
        return <span style={{ color, fontWeight: 500 }}>{val?.toFixed(1)}%</span>;
      },
    },
    {
      title: t('prompt.actions') || '操作',
      key: 'actions',
      width: 160,
      render: (_: unknown, record: PromptVersion) => (
        <Space size="small">
          <Tooltip title={t('common.edit') || '编辑'}>
            <Button size="small" icon={<EditOutlined />} onClick={() => handleEdit(record)} />
          </Tooltip>
          {!record.is_active && (
            <Tooltip title={t('prompt.setActive') || '设为激活'}>
              <Button size="small" type="primary" icon={<CheckCircleOutlined />} onClick={() => handleActivate(record.feature)} />
            </Tooltip>
          )}
          {record.is_active && (
            <Popconfirm
              title={t('prompt.deactivateConfirm') || '确认停用此版本？'}
              onConfirm={() => handleDeactivate(record.id)}
              okText={t('common.confirm') || '确认'}
              cancelText={t('common.cancel') || '取消'}
            >
              <Tooltip title={t('prompt.deactivate') || '停用'}>
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Tooltip>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '20px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <ExperimentOutlined style={{ fontSize: 22, color: 'var(--fst-primary)' }} />
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: 'var(--fst-on-surface)' }}>
          {t('settings.promptManagementTab') || 'Prompt 管理'}
        </h2>
      </div>
      <p style={{ margin: '0 0 16px', color: 'var(--fst-on-surface-muted)', fontSize: 13 }}>
        {t('settings.promptManagementDesc') || '管理各 AI 功能的 Prompt 版本、A/B 流量权重与效果统计'}
      </p>

      <Card style={{ borderRadius: 14 }}>
        <div style={{ padding: '8px 0' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <Space>
              <Select
                value={filterFeature || ''}
                onChange={(v) => setFilterFeature(v || undefined)}
                options={featureOptions}
                style={{ width: 180 }}
                placeholder={t('prompt.filterFeature') || '筛选功能'}
              />
              <Tooltip title={t('prompt.refreshStats') || '刷新统计'}>
                <Button icon={<ReloadOutlined />} onClick={handleRefreshStats} />
              </Tooltip>
            </Space>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleCreate}>
              {t('prompt.createVersion') || '新建版本'}
            </Button>
          </div>

          <Table
            rowKey="id"
            columns={columns}
            dataSource={versions}
            loading={loading}
            size="small"
            pagination={{
              current: page,
              total,
              pageSize: 10,
              onChange: (p) => { setPage(p); loadVersions(p); },
              showTotal: (t) => `${t} 条`,
              showSizeChanger: false,
            }}
          />

          {/* 创建/编辑弹窗 */}
          <Modal
            title={editingVersion ? (t('prompt.editVersion') || '编辑 Prompt 版本') : (t('prompt.createVersion') || '新建 Prompt 版本')}
            open={modalOpen}
            onCancel={() => setModalOpen(false)}
            onOk={handleSave}
            confirmLoading={saving}
            width={720}
            okText={t('common.save') || '保存'}
            cancelText={t('common.cancel') || '取消'}
          >
            <Form form={form} layout="vertical" style={{ marginTop: 16 }}>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item
                    label={t('prompt.feature') || '功能模块'}
                    name="feature"
                    rules={[{ required: true, message: t('prompt.featureRequired') || '请选择功能模块' }]}
                  >
                    <Select
                      options={PROMPT_FEATURES.map(f => ({ label: f.label, value: f.value }))}
                      placeholder={t('prompt.selectFeature') || '选择功能'}
                      disabled={!!editingVersion}
                    />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item
                    label={t('prompt.name') || '版本名称'}
                    name="name"
                    rules={[{ required: true, message: t('prompt.nameRequired') || '请输入版本名称' }]}
                  >
                    <Input placeholder={t('prompt.namePlaceholder') || '例如：v1、baseline、experiment-A'} />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item
                label={t('prompt.systemPrompt') || '系统提示词'}
                name="system_prompt"
                rules={[{ required: true, message: t('prompt.systemPromptRequired') || '请输入系统提示词' }]}
              >
                <Input.TextArea rows={6} placeholder={t('prompt.systemPromptPlaceholder') || '输入 Prompt 内容...'} style={{ fontFamily: 'monospace' }} />
              </Form.Item>

              <Form.Item
                label={t('prompt.userPromptTemplate') || '用户提示词模板'}
                name="user_prompt_template"
              >
                <Input.TextArea rows={3} placeholder={t('prompt.userPromptPlaceholder') || '可选，支持 {variable} 占位符'} style={{ fontFamily: 'monospace' }} />
              </Form.Item>

              <Row gutter={16}>
                <Col span={8}>
                  <Form.Item label={t('prompt.temperature') || '温度'} name="temperature">
                    <InputNumber min={0} max={2} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item label={t('prompt.trafficWeight') || '流量权重'} name="traffic_weight">
                    <InputNumber min={0} max={1} step={0.1} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
                <Col span={8}>
                  <Form.Item label={t('prompt.modelName') || '指定模型'} name="model_name">
                    <Input placeholder={t('prompt.modelPlaceholder') || '留空使用全局默认'} />
                  </Form.Item>
                </Col>
              </Row>

              <Form.Item label={t('prompt.changeNotes') || '变更说明'} name="change_notes">
                <Input.TextArea rows={2} placeholder={t('prompt.changeNotesPlaceholder') || '本次变更的说明（可选）'} />
              </Form.Item>

              <Form.Item label={t('prompt.isActive') || '激活'} name="is_active" valuePropName="checked">
                <Switch />
              </Form.Item>
            </Form>
          </Modal>
        </div>
      </Card>
    </div>
  )
}

export default PromptManagement
