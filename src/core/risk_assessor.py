"""
F11: Security Risk Summary

Operator-facing risk translation with next-step guidance.
Converts technical security findings into plain-language guidance.
"""

import logging
from typing import Dict, List
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

logger = logging.getLogger("nexus-risk")


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    MITIGATED = "mitigated"


@dataclass
class RiskFinding:
    id: str
    category: str
    title: str
    description: str
    level: RiskLevel
    impact: str
    guidance: str
    detected_at: str = ""

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = datetime.now().isoformat()


class RiskAssessor:
    """Translates technical security data into operator guidance"""

    RISK_CATEGORIES = {
        "credentials": "Credenciales expuestas",
        "network": "Exposicion de red",
        "api": "Configuracion de API",
        "data": "Proteccion de datos",
        "access": "Control de acceso",
        "config": "Configuracion insegura",
    }

    def __init__(self):
        self._findings: List[RiskFinding] = []

    def assess_system(self, system_state: Dict) -> List[RiskFinding]:
        """Assess system state and generate risk findings"""
        self._findings = []

        # Check for exposed credentials
        env_vars = system_state.get("env_vars", {})
        for var, value in env_vars.items():
            if any(kw in var.lower() for kw in ["password", "secret", "token", "key"]) and value:
                if len(value) < 8 or value in ("password", "123456", "admin"):
                    masked_var = var[:3] + "***" + var[-2:] if len(var) > 5 else "***"
                    self._add_finding(
                        category="credentials",
                        title="Contraseña debil detectada",
                        description=f"Variable de credencial ({masked_var}) tiene valor debil",
                        level=RiskLevel.HIGH,
                        impact="Credenciales pueden ser adivinadas por atacantes",
                        guidance="Cambiar a una contraseña fuerte de al menos 16 caracteres con simbolos",
                    )

        # Check network exposure
        ports = system_state.get("open_ports", [])
        public_ports = [p for p in ports if p.get("bind") in ("0.0.0.0", "::")]
        if public_ports:
            self._add_finding(
                category="network",
                title="Servicios expuestos publicamente",
                description=f"{len(public_ports)} servicios escuchan en 0.0.0.0",
                level=RiskLevel.MEDIUM,
                impact="Servicios accesibles desde cualquier red",
                guidance="Configurar servicios para escuchar en 127.0.0.1 a menos que sea intencional",
            )

        # Check API security
        api_config = system_state.get("api_config", {})
        if not api_config.get("auth_enabled", True):
            self._add_finding(
                category="api",
                title="API sin autenticacion",
                description="La API no requiere autenticacion",
                level=RiskLevel.CRITICAL,
                impact="Cualquier usuario puede ejecutar operaciones",
                guidance="Habilitar autenticacion con token en todos los endpoints de escritura",
            )

        if api_config.get("cors_wildcard", False):
            self._add_finding(
                category="api",
                title="CORS wildcard habilitado",
                description="CORS permite cualquier origen",
                level=RiskLevel.MEDIUM,
                impact="Sitios maliciosos pueden hacer requests a la API",
                guidance="Restringir CORS a origenes especificos",
            )

        # Check data protection
        db_config = system_state.get("database", {})
        if not db_config.get("encryption", False):
            self._add_finding(
                category="data",
                title="Base de datos sin encriptacion",
                description="Las bases de datos no estan encriptadas en reposo",
                level=RiskLevel.LOW,
                impact="Datos accesibles si alguien obtiene acceso al disco",
                guidance="Considerar encriptacion SQLite con SQLCipher para datos sensibles",
            )

        # Check access control
        if system_state.get("admin_no_auth", False):
            self._add_finding(
                category="access",
                title="Admin sin proteccion",
                description="Funciones administrativas no requieren autenticacion",
                level=RiskLevel.CRITICAL,
                impact="Cualquier usuario puede modificar configuracion critica",
                guidance="Proteger todas las funciones admin con autenticacion y autorizacion",
            )

        # Check debug mode
        if system_state.get("debug_mode", False):
            self._add_finding(
                category="config",
                title="Modo debug activo",
                description="El modo debug esta habilitado en produccion",
                level=RiskLevel.HIGH,
                impact="Informacion interna expuesta en respuestas de error",
                guidance="Desactivar modo debug en entornos de produccion",
            )

        return self._findings

    def _add_finding(self, category: str, title: str, description: str, level: RiskLevel, impact: str, guidance: str):
        import uuid
        self._findings.append(RiskFinding(
            id=str(uuid.uuid4())[:8],
            category=category,
            title=title,
            description=description,
            level=level,
            impact=impact,
            guidance=guidance,
        ))

    def get_summary(self) -> Dict:
        by_level = {}
        for level in RiskLevel:
            findings = [f for f in self._findings if f.level == level]
            by_level[level.value] = [
                {"id": f.id, "title": f.title, "guidance": f.guidance}
                for f in findings
            ]

        critical_count = sum(1 for f in self._findings if f.level == RiskLevel.CRITICAL)
        overall = "critical" if critical_count > 0 else (
            "high" if any(f.level == RiskLevel.HIGH for f in self._findings) else (
                "medium" if any(f.level == RiskLevel.MEDIUM for f in self._findings) else "low"
            )
        )

        return {
            "overall_risk": overall,
            "total_findings": len(self._findings),
            "by_level": {k: len(v) for k, v in by_level.items()},
            "findings": by_level,
            "next_steps": self._get_next_steps(),
        }

    def _get_next_steps(self) -> List[str]:
        steps = []
        critical = [f for f in self._findings if f.level == RiskLevel.CRITICAL]
        high = [f for f in self._findings if f.level == RiskLevel.HIGH]

        if critical:
            steps.append(f"URGENTE: Resolver {len(critical)} riesgo(s) critico(s) inmediatamente")
        if high:
            steps.append(f"Resolver {len(high)} riesgo(s) alto(s) en las proximas 24 horas")
        if self._findings:
            steps.append("Revisar guia de cada hallazgo para instrucciones especificas")
        else:
            steps.append("No se encontraron riesgos. Sistema seguro.")

        return steps

    def get_stats(self) -> Dict:
        return {
            "total_findings": len(self._findings),
            "categories_assessed": len(self.RISK_CATEGORIES),
        }

    def get_risks_by_level(self, level: str) -> List[Dict]:
        """Get findings filtered by risk level"""
        level_map = {"critical": RiskLevel.CRITICAL, "high": RiskLevel.HIGH, "medium": RiskLevel.MEDIUM, "low": RiskLevel.LOW}
        target = level_map.get(level.lower())
        if not target:
            return []
        return [
            {"id": f.id, "title": f.title, "description": f.description, "guidance": f.guidance}
            for f in self._findings if f.level == target
        ]

    def mitigate_risk(self, finding_index: int):
        """Mark a finding as mitigated"""
        if 0 <= finding_index < len(self._findings):
            self._findings[finding_index].level = RiskLevel.MITIGATED
