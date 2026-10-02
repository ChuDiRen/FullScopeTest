import { useEffect, useState } from 'react'
import { Button, Card, Col, Divider, Form, Input, Row, Typography, message } from 'antd'
import { RobotOutlined, SaveOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { apiTestService } from '../services/apiTestService'
import { useRole } from '../hooks/useRole'

const { Text } = Typography

/**
 * AI 助手全局配置页 —— 自系统设置迁入「AI 助手」分组（/ai-assistant/config）。
 * 配置的大模型参数全局生效：AI 接口生成、Web 测试探索引擎、性能测试场景生成
 * 以及全局悬浮 Copilot。
 */
const AiAssistantConfig = () => {
  const { t } = useTranslation()
  const { isAdmin } = useRole()
  const [form] = Form.useForm()
  const [loading, setLoading] = useState(false)
  const [globalAiConfig, setGlobalAiConfig] = useState<any>(null)

  useEffect(() => {
    apiTestService.getAiConfig()
      .then((res: any) => {
        if (res.code === 200 && res.data) setGlobalAiConfig(res.data)
      })
      .catch(() => {})

    form.setFieldsValue({
      aiBaseUrl: localStorage.getItem('api-test-ai-base-url') || '',
      aiModel: localStorage.getItem('api-test-ai-model') || '',
      aiApiKey: localStorage.getItem('api-test-ai-api-key') || '',
    })
  }, [form])

  const handleSaveAi = async (values: any) => {
    setLoading(true)
    try {
      const payload = {
        base_url: values.aiBaseUrl || '',
        model: values.aiModel || '',
        api_key: values.aiApiKey || '',
      }
      const res = await apiTestService.saveAiConfig(payload)
      if (res.code !== 200) {
        message.error(res.message || t('common.failed'))
        return
      }
      Object.entries(payload).forEach(([k, v]) => {
        const lk = `api-test-ai-${k.replace(/_/g, '-')}`
        localStorage.setItem(lk, v as string)
      })
      if (res.data) setGlobalAiConfig(res.data)
      message.success(t('settings.saveSuccess'))
    } catch {
      message.error(t('common.failed'))
    } finally {
      setLoading(false)
    }
  }

  const labelStyle = { fontWeight: 600, fontSize: 13 }

  const hintBox = (text: string) => (
    <div style={{
      padding: '10px 14px',
      borderRadius: 'var(--fst-radius-lg)',
      background: 'rgba(45, 106, 100, 0.06)',
      border: '1px solid rgba(45, 106, 100, 0.18)',
      color: 'var(--fst-on-surface-variant)',
      fontSize: 13,
      marginBottom: 16,
    }}>
      {text}
    </div>
  )

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '20px 24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 }}>
        <RobotOutlined style={{ fontSize: 22, color: 'var(--fst-primary)' }} />
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 700, color: 'var(--fst-on-surface)' }}>
          {t('settings.aiConfig') || 'AI 助手配置 (全局)'}
        </h2>
      </div>
      <p style={{ margin: '0 0 16px', color: 'var(--fst-on-surface-muted)', fontSize: 13 }}>
        {t('settings.aiConfigDesc') || '配置全局大模型参数，对所有 AI 功能生效'}
      </p>

      <Card style={{ borderRadius: 14 }}>
        <div style={{ padding: '8px 0' }}>
          {hintBox(t('settings.aiConfigHint'))}

          <Form
            form={form}
            layout="vertical"
            onFinish={handleSaveAi}
            initialValues={{
              aiBaseUrl: '',
              aiModel: '',
              aiApiKey: ''
            }}
          >
            <Row gutter={24}>
              <Col span={12}>
                <Form.Item label={<span style={labelStyle}>Base URL</span>} name="aiBaseUrl" rules={[{ required: true, message: t('settings.baseURLRequired') }]} tooltip={t('settings.baseURLTooltip')}>
                  <Input placeholder={globalAiConfig?.base_url || "https://api.openai.com/v1"} />
                </Form.Item>
              </Col>
              <Col span={12}>
                <Form.Item label={<span style={labelStyle}>{t('settings.modelLabel') || '模型名称'}</span>} name="aiModel" rules={[{ required: true, message: t('settings.modelRequired') }]} tooltip={t('settings.modelTooltip')}>
                  <Input placeholder={globalAiConfig?.model || "gpt-4o-mini"} />
                </Form.Item>
              </Col>
            </Row>

            <Row gutter={24}>
              <Col span={12}>
                <Form.Item label={<span style={labelStyle}>API Key</span>} name="aiApiKey" rules={[{ required: true, message: t('settings.apiKeyRequired') }]}>
                  <Input.Password placeholder={globalAiConfig?.api_key || "sk-..."} />
                </Form.Item>
              </Col>
            </Row>

            <Divider style={{ margin: '16px 0' }} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('settings.visionMergedHint') || '视觉分析（Web 测试截图理解等）直接使用上方主模型，无需单独配置'}
            </Text>

            <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={loading} disabled={!isAdmin}>
              {t('settings.saveBtn')}
            </Button>
          </Form>
        </div>
      </Card>
    </div>
  )
}

export default AiAssistantConfig
