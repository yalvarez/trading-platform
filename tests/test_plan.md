# Plan de Pruebas Integral - Plataforma Trading

## 1. Parsers de Señales
- [ ] Cada parser reconoce solo su formato y rechaza los demás
- [ ] Casos límite: señales incompletas, campos faltantes, formatos ambiguos
- [ ] Señales con “Risk Price” solo las reconoce LimitlessParser

## 2. Deduplicación
- [ ] Una señal repetida no se procesa dos veces
- [ ] Señales similares pero con cambios relevantes sí se procesan

## 3. Lógica de Trading
- [ ] Ejecución correcta de trades con todos los parámetros (SL, TP, volumen, dirección)
- [ ] Manejo de errores: autotrading deshabilitado, conexión rechazada, retcodes de MT5
- [ ] Cierre parcial y breakeven se ejecutan en los escenarios correctos

## 4. Endpoints Backend
- [ ] CRUD de proveedores, cuentas, configuraciones y permisos
- [ ] Validaciones de campos obligatorios y tipos de datos
- [ ] Respuestas ante datos inválidos o duplicados

## 5. Integración E2E
- [ ] Flujo completo: señal → parsing → deduplicación → ejecución de trade → registro
- [ ] Simulación de señales desde diferentes canales y proveedores
- [ ] Verificación de logs y estados finales

## 6. Seguridad y Permisos
- [ ] Acceso restringido a endpoints según roles
- [ ] Pruebas de autenticación y autorización

## 7. Monitoreo y Logging
- [ ] Verificar que los logs se generan correctamente en cada etapa
- [ ] Alertas y métricas Prometheus disponibles y correctas
