import React, { useState, useEffect, useCallback } from 'react';
import { Card, Form, Input, Button, message, Typography, Row, Col, Space, Tabs, Switch, Select, Slider, Divider, Radio, Tag, Alert, Popconfirm, Table, Modal, InputNumber, Tooltip, Badge } from 'antd';
import {
  SaveOutlined, SettingOutlined, BulbOutlined,
  GlobalOutlined, BellOutlined, SafetyOutlined, KeyOutlined,
  SunOutlined, MoonOutlined, DesktopOutlined,
  LinkOutlined, GithubOutlined, DisconnectOutlined, ApiOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { useThemeStore } from '../stores/themeStore';
import { useBranding } from '../hooks/useBranding';
import { useRole } from '../hooks/useRole';
import api from '../services/api';

const { Text } = Typography;

const Settings: React.FC = () => {
  const { t, i18n } = useTranslation();
  const { isAdmin } = useRole();
  const [loading, setLoading] = useState(false);
  const { mode: themeMode, setMode: setThemeMode, resolvedTheme } = useThemeStore();

  // General settings state
  const [pageSize, setPageSize] = useState(() => Number(localStorage.getItem('fst-page-size') || 20));
  const [autoRefresh, setAutoRefresh] = useState(() => localStorage.getItem('fst-auto-refresh') === 'true');
  const [refreshInterval, setRefreshInterval] = useState(() => Number(localStorage.getItem('fst-refresh-interval') || 30));
  const [reduceMotion, setReduceMotion] = useState(() => localStorage.getItem('fst-reduce-motion') === 'true');

  const handleSaveGeneral = () => {
    localStorage.setItem('fst-page-size', String(pageSize));
    localStorage.setItem('fst-auto-refresh', String(autoRefresh));
    localStorage.setItem('fst-refresh-interval', String(refreshInterval));
    localStorage.setItem('fst-reduce-motion', String(reduceMotion));
    if (reduceMotion) {
      document.documentElement.style.setProperty('--animation-duration', '0s');
    } else {
      document.documentElement.style.removeProperty('--animation-duration');
    }
    message.success(t('settings.saveSuccess'));
  };

  const handleLanguageChange = (lang: string) => {
    i18n.changeLanguage(lang);
    localStorage.setItem('fst-language', lang);
    message.success(t('settings.saveSuccess'));
  };

  const cardStyle = {
    marginBottom: 0,
  };

  const labelStyle = { fontWeight: 600, fontSize: 13 };

  /** P32-1: 品牌定制子组件 */
  const BrandingTab: React.FC = () => {
    const { branding } = useBranding();
    const [form] = Form.useForm();
    const [saving, setSaving] = useState(false);

    useEffect(() => {
      form.setFieldsValue({
        platform_name: branding.platform_name,
        logo_url: branding.logo_url || '',
        favicon_url: branding.favicon_url || '',
        primary_color: branding.primary_color,
        footer_text: branding.footer_text || '',
      });
    }, [branding, form]);

    const handleSave = async () => {
      try {
        const values = await form.validateFields();
        setSaving(true);
        const payload: Record<string, string> = {};
        if (values.platform_name) payload.platform_name = values.platform_name;
        if (values.logo_url) payload.logo_url = values.logo_url;
        if (values.favicon_url) payload.favicon_url = values.favicon_url;
        if (values.primary_color) payload.primary_color = values.primary_color;
        if (values.footer_text) payload.footer_text = values.footer_text;
        const res = await api.put('/branding/config', payload);
        if ((res as any).code === 200 || (res as any).data?.code === 200) {
          message.success(t('branding.saveSuccess') || '品牌配置已更新');
        }
      } catch {
        message.error(t('branding.saveFailed') || '保存失败');
      } finally {
        setSaving(false);
      }
    };

    return (
      <div style={{ padding: '8px 0' }}>
        <Form form={form} layout="vertical">
          <Form.Item label={t('branding.platformName') || '平台名称'} name="platform_name">
            <Input placeholder="大熊AI测试平台" />
          </Form.Item>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label={t('branding.logoUrl') || 'Logo URL'} name="logo_url">
                <Input placeholder="/logo-full.webp" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label={t('branding.faviconUrl') || 'Favicon URL'} name="favicon_url">
                <Input placeholder="/favicon.ico" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label={t('branding.primaryColor') || '主色调'} name="primary_color">
                <Input placeholder="#5FA59B" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item label={t('branding.footerText') || 'Footer 文案'} name="footer_text">
                <Input placeholder={t('branding.footerPlaceholder') || '© 2026 Your Company'} />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" icon={<SaveOutlined />} onClick={handleSave} loading={saving}>
            {t('settings.saveBtn')}
          </Button>
        </Form>
      </div>
    );
  };

  const hintBox = (text: string) => (
    <div style={{
      padding: '10px 14px',
      borderRadius: 'var(--fst-radius-lg)',
      background: resolvedTheme === 'dark' ? 'rgba(74, 158, 150, 0.08)' : 'rgba(45, 106, 100, 0.06)',
      border: `1px solid ${resolvedTheme === 'dark' ? 'rgba(74, 158, 150, 0.15)' : 'rgba(45, 106, 100, 0.12)'}`,
      fontSize: 13,
      color: 'var(--fst-on-surface-variant)',
      marginBottom: 20,
      lineHeight: 1.6,
    }}>
      {text}
    </div>
  );

  const tabItems = [
    {
      key: 'general',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <SettingOutlined /> {t('settings.generalTab') || '通用'}
        </span>
      ),
      children: (
        <div style={{ padding: '8px 0' }}>
          {hintBox(t('settings.generalHint') || '配置通用的平台行为偏好，包括分页大小、自动刷新和动画效果。')}

          <div style={{ marginBottom: 20 }}>
            <Text strong style={labelStyle}>{t('settings.language') || '语言'}</Text>
            <div style={{ marginTop: 8 }}>
              <Select
                value={i18n.language}
                onChange={handleLanguageChange}
                style={{ width: 200 }}
                options={[
                  { label: '简体中文', value: 'zh' },
                  { label: 'English', value: 'en' },
                ]}
              />
            </div>
          </div>

          <Divider style={{ margin: '16px 0' }} />

          <div style={{ marginBottom: 20 }}>
            <Text strong style={labelStyle}>{t('settings.pageSize') || '默认分页大小'}</Text>
            <div style={{ marginTop: 8 }}>
              <Slider
                min={10}
                max={100}
                step={10}
                value={pageSize}
                onChange={setPageSize}
                marks={{ 10: '10', 20: '20', 50: '50', 100: '100' }}
                style={{ maxWidth: 400 }}
              />
            </div>
          </div>

          <Divider style={{ margin: '16px 0' }} />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
            <div>
              <Text strong style={labelStyle}>{t('settings.autoRefresh') || '自动刷新'}</Text>
              <div style={{ fontSize: 12, color: 'var(--fst-on-surface-muted)', marginTop: 2 }}>
                {t('settings.autoRefreshHint') || '自动刷新页面数据'}
              </div>
            </div>
            <Switch checked={autoRefresh} onChange={setAutoRefresh} />
          </div>

          {autoRefresh && (
            <div style={{ marginBottom: 16, paddingLeft: 0 }}>
              <Text style={{ fontSize: 13 }}>{t('settings.refreshInterval') || '刷新间隔'}: {refreshInterval}s</Text>
              <Slider
                min={10}
                max={120}
                step={10}
                value={refreshInterval}
                onChange={setRefreshInterval}
                marks={{ 10: '10s', 30: '30s', 60: '60s', 120: '120s' }}
                style={{ maxWidth: 400, marginTop: 8 }}
              />
            </div>
          )}

          <Divider style={{ margin: '16px 0' }} />

          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
            <div>
              <Text strong style={labelStyle}>{t('settings.reduceMotion') || '减少动画'}</Text>
              <div style={{ fontSize: 12, color: 'var(--fst-on-surface-muted)', marginTop: 2 }}>
                {t('settings.reduceMotionHint') || '关闭过渡动画，提升无障碍体验'}
              </div>
            </div>
            <Switch checked={reduceMotion} onChange={setReduceMotion} />
          </div>

          <Button type="primary" icon={<SaveOutlined />} onClick={handleSaveGeneral}>
            {t('settings.saveBtn')}
          </Button>
        </div>
      ),
    },
    {
      key: 'appearance',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <BulbOutlined /> {t('settings.appearanceTab') || '外观'}
        </span>
      ),
      children: (
        <div style={{ padding: '8px 0' }}>
          {hintBox(t('settings.appearanceHint') || '自定义界面外观主题，支持亮色、暗色和跟随系统三种模式。')}

          <Text strong style={labelStyle}>{t('settings.theme') || '主题模式'}</Text>
          <div style={{ marginTop: 12 }}>
            <Radio.Group
              value={themeMode}
              onChange={(e) => setThemeMode(e.target.value)}
              buttonStyle="solid"
              size="large"
            >
              <Radio.Button value="light">
                <SunOutlined style={{ marginRight: 6 }} />{t('settings.light') || '亮色'}
              </Radio.Button>
              <Radio.Button value="dark">
                <MoonOutlined style={{ marginRight: 6 }} />{t('settings.dark') || '暗色'}
              </Radio.Button>
              <Radio.Button value="system">
                <DesktopOutlined style={{ marginRight: 6 }} />{t('settings.system') || '跟随系统'}
              </Radio.Button>
            </Radio.Group>
          </div>

          <div style={{
            marginTop: 24,
            padding: 16,
            borderRadius: 12,
            background: 'var(--fst-surface-variant, #f5f5f5)',
            border: '1px solid var(--fst-outline-soft, #e8e8e8)',
          }}>
            <Text style={{ fontSize: 13, color: 'var(--fst-on-surface-variant)' }}>
              {resolvedTheme === 'dark'
                ? (t('settings.darkPreview') || '当前为暗色模式，深色背景可减少眼部疲劳，适合夜间使用。')
                : (t('settings.lightPreview') || '当前为亮色模式，适合日间使用。')}
            </Text>
          </div>
        </div>
      ),
    },
    {
      key: 'security',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <SafetyOutlined /> {t('settings.securityTab') || '安全'}
        </span>
      ),
      children: (
        <div style={{ padding: '8px 0' }}>
          {hintBox(t('settings.securityHint') || '管理密码策略、登录安全和会话超时设置。')}

          <div style={{ marginBottom: 20 }}>
            <Text strong style={labelStyle}>{t('settings.passwordPolicy') || '密码策略'}</Text>
            <div style={{ marginTop: 8, padding: 16, borderRadius: 12, background: 'var(--fst-surface-variant, #fafafa)', border: '1px solid var(--fst-outline-soft, #f0f0f0)' }}>
              <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13, color: 'var(--fst-on-surface-variant)', lineHeight: 2 }}>
                <li>{t('settings.pwdMinLength') || '最少 8 个字符'}</li>
                <li>{t('settings.pwdRequireUpper') || '必须包含大写字母'}</li>
                <li>{t('settings.pwdRequireLower') || '必须包含小写字母'}</li>
                <li>{t('settings.pwdRequireDigit') || '必须包含数字'}</li>
                <li>{t('settings.pwdRequireSpecial') || '必须包含特殊字符'}</li>
              </ul>
            </div>
          </div>

          <Divider style={{ margin: '16px 0' }} />

          <div style={{ marginBottom: 20 }}>
            <Text strong style={labelStyle}>{t('settings.sessionTimeout') || '会话超时'}</Text>
            <div style={{ marginTop: 8 }}>
              <Select
                defaultValue="24h"
                style={{ width: 200 }}
                disabled={!isAdmin}
                options={[
                  { label: '1 小时', value: '1h' },
                  { label: '4 小时', value: '4h' },
                  { label: '8 小时', value: '8h' },
                  { label: '24 小时', value: '24h' },
                  { label: '7 天', value: '7d' },
                  { label: '30 天', value: '30d' },
                ]}
              />
            </div>
          </div>

          <Divider style={{ margin: '16px 0' }} />

          <div style={{ marginBottom: 20 }}>
            <Text strong style={labelStyle}>{t('settings.loginAttempts') || '登录失败锁定'}</Text>
            <div style={{ marginTop: 8 }}>
              <Select
                defaultValue="5"
                style={{ width: 200 }}
                options={[
                  { label: '3 次失败后锁定', value: '3' },
                  { label: '5 次失败后锁定', value: '5' },
                  { label: '10 次失败后锁定', value: '10' },
                  { label: '不限制', value: '0' },
                ]}
              />
            </div>
          </div>

          <div style={{
            padding: 16,
            borderRadius: 12,
            background: resolvedTheme === 'dark' ? 'rgba(232, 112, 108, 0.08)' : 'rgba(232, 112, 108, 0.06)',
            border: `1px solid ${resolvedTheme === 'dark' ? 'rgba(232, 112, 108, 0.2)' : 'rgba(232, 112, 108, 0.15)'}`,
            marginTop: 16,
          }}>
            <Text style={{ fontSize: 13, color: 'var(--fst-error, #E8706C)' }}>
              {t('settings.securityWarning') || '安全设置修改需要管理员权限，修改后立即生效。'}
            </Text>
          </div>
        </div>
      ),
    },
    {
      key: 'notifications',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <BellOutlined /> {t('settings.notificationsTab') || '通知'}
        </span>
      ),
      children: (
        <div style={{ padding: '8px 0' }}>
          {hintBox(t('settings.notificationsHint') || '配置通知渠道和订阅事件，支持钉钉、飞书、Slack 等平台。')}

          {[
            { label: t('settings.notifyTestComplete') || '测试完成通知', desc: t('settings.notifyTestCompleteDesc') || '当测试执行完成时发送通知', key: 'test_complete' },
            { label: t('settings.notifyTestFailed') || '测试失败通知', desc: t('settings.notifyTestFailedDesc') || '当测试执行失败时发送通知', key: 'test_failed' },
            { label: t('settings.notifyAlert') || '性能告警通知', desc: t('settings.notifyAlertDesc') || '当性能指标超过阈值时发送通知', key: 'alert_triggered' },
            { label: t('settings.notifyQualityGate') || '质量门禁通知', desc: t('settings.notifyQualityGateDesc') || '当质量门禁评估失败时发送通知', key: 'quality_gate_failed' },
          ].map((item, index) => (
            <div key={item.key} style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              padding: '12px 0',
              borderBottom: index < 3 ? '1px solid var(--fst-outline-soft, #f0f0f0)' : 'none',
            }}>
              <div>
                <Text strong style={{ fontSize: 14 }}>{item.label}</Text>
                <div style={{ fontSize: 12, color: 'var(--fst-on-surface-muted, #999)', marginTop: 2 }}>{item.desc}</div>
              </div>
              <Switch defaultChecked />
            </div>
          ))}

          <Divider style={{ margin: '16px 0' }} />

          <Button type="primary" icon={<SaveOutlined />} onClick={() => message.success(t('settings.saveSuccess'))}>
            {t('settings.saveBtn')}
          </Button>
        </div>
      ),
    },
    {
      key: 'branding',
      label: (
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <GlobalOutlined /> {t('settings.brandingTab') || '品牌定制'}
        </span>
      ),
      children: (
        <BrandingTab />
      ),
    },
  ];

  return (
    <div className="fst-page" style={{ maxWidth: 1000, margin: '0 auto' }}>
      <div className="fst-page-header fst-animate-in">
        <div>
          <h1 className="fst-page-title">{t('settings.title')}</h1>
          <div className="fst-ios-card-subtitle">{t('settings.platformSubtitle')}</div>
        </div>
      </div>

      <div className="fst-ios-card fst-animate-in fst-animate-in-1">
        <Tabs
          items={tabItems}
          tabPosition="left"
          style={{ minHeight: 500 }}
          tabBarStyle={{ width: 160, marginRight: 0 }}
        />
      </div>
    </div>
  );
};

export default Settings;
