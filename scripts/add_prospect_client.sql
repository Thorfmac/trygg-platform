INSERT INTO platform.clients (id, name, contact_email, telegram_chat_id, tier, is_active, metadata)
VALUES (
    gen_random_uuid(),
    'Prospect IFA - HNW',
    'prospect@trygg.demo',
    '8639424741',
    'premium',
    TRUE,
    '{"firm_profile": {"firm_type": "ifa", "description": "Established IFA firm providing full financial planning to high net worth clients", "regulatory_permissions": ["investment_advice", "pension_transfers", "arranging_deals_in_investments", "retail_investment_products", "mortgage_advice"], "client_types": ["high_net_worth"], "key_risk_areas": ["consumer_duty", "smcr", "suitability", "pension_transfers", "operational_resilience", "data_protection", "ai_governance", "inheritance_tax_planning"], "assets_under_advice": "100m_to_500m", "uses_discretionary_management": false, "regulated_by": ["fca"]}}'::jsonb
);
