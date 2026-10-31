"""AutoCAD Color Index (ACI) 1-255, True Color RGB, and Layer Color Resolver.

Preserves exact AutoCAD / Navisworks visual appearance across DWG, DXF, STEP, and IFC.
"""
from typing import Dict, Tuple, Optional

# Canonical 256-entry AutoCAD Color Index (ACI) to RGB table
ACI_TABLE: Dict[int, Tuple[int, int, int]] = {
    0: (0, 0, 0),         # ByBlock placeholder
    1: (255, 0, 0),       # Red
    2: (255, 255, 0),     # Yellow
    3: (0, 255, 0),       # Green
    4: (0, 255, 255),     # Cyan
    5: (0, 0, 255),       # Blue
    6: (255, 0, 255),     # Magenta
    7: (215, 220, 225),   # Standard neutral viewport light slate gray (never blinding white)
    8: (128, 128, 128),   # Dark gray
    9: (192, 192, 192),   # Light gray
    10: (255, 0, 0), 11: (255, 127, 127), 12: (204, 0, 0), 13: (204, 102, 102),
    14: (153, 0, 0), 15: (153, 76, 76), 16: (127, 0, 0), 17: (127, 63, 63),
    18: (76, 0, 0), 19: (76, 38, 38), 20: (255, 63, 0), 21: (255, 159, 127),
    22: (204, 51, 0), 23: (204, 127, 102), 24: (153, 38, 0), 25: (153, 95, 76),
    26: (127, 31, 0), 27: (127, 79, 63), 28: (76, 19, 0), 29: (76, 47, 38),
    30: (255, 127, 0), 31: (255, 191, 127), 32: (204, 102, 0), 33: (204, 153, 102),
    34: (153, 76, 0), 35: (153, 114, 76), 36: (127, 63, 0), 37: (127, 95, 63),
    38: (76, 38, 0), 39: (76, 57, 38), 40: (255, 191, 0), 41: (255, 223, 127),
    42: (204, 153, 0), 43: (204, 178, 102), 44: (153, 114, 0), 45: (153, 133, 76),
    46: (127, 95, 0), 47: (127, 111, 63), 48: (76, 57, 0), 49: (76, 66, 38),
    50: (255, 255, 0), 51: (255, 255, 127), 52: (204, 204, 0), 53: (204, 204, 102),
    54: (153, 153, 0), 55: (153, 153, 76), 56: (127, 127, 0), 57: (127, 127, 63),
    58: (76, 76, 0), 59: (76, 76, 38), 60: (191, 255, 0), 61: (223, 255, 127),
    62: (153, 204, 0), 63: (178, 204, 102), 64: (114, 153, 0), 65: (133, 153, 76),
    66: (95, 127, 0), 67: (111, 127, 63), 68: (57, 76, 0), 69: (66, 76, 38),
    70: (127, 255, 0), 71: (191, 255, 127), 72: (102, 204, 0), 73: (153, 204, 102),
    74: (76, 153, 0), 75: (114, 153, 76), 76: (63, 127, 0), 77: (95, 127, 63),
    78: (38, 76, 0), 79: (57, 76, 38), 80: (63, 255, 0), 81: (159, 255, 127),
    82: (51, 204, 0), 83: (127, 204, 102), 84: (38, 153, 0), 85: (95, 153, 76),
    86: (31, 127, 0), 87: (79, 127, 63), 88: (19, 76, 0), 89: (47, 76, 38),
    90: (0, 255, 0), 91: (127, 255, 127), 92: (0, 204, 0), 93: (102, 204, 102),
    94: (0, 153, 0), 95: (76, 153, 76), 96: (0, 127, 0), 97: (63, 127, 63),
    98: (0, 76, 0), 99: (38, 76, 38), 100: (0, 255, 63), 101: (127, 255, 159),
    102: (0, 204, 51), 103: (102, 204, 127), 104: (0, 153, 38), 105: (76, 153, 95),
    106: (0, 127, 31), 107: (63, 127, 79), 108: (0, 76, 19), 109: (38, 76, 47),
    110: (0, 255, 127), 111: (127, 255, 191), 112: (0, 204, 102), 113: (102, 204, 153),
    114: (0, 153, 76), 115: (76, 153, 114), 116: (0, 127, 63), 117: (63, 127, 95),
    118: (0, 76, 38), 119: (38, 76, 57), 120: (0, 255, 191), 121: (127, 255, 223),
    122: (0, 204, 153), 123: (102, 204, 178), 124: (0, 153, 114), 125: (76, 153, 133),
    126: (0, 127, 95), 127: (63, 127, 111), 128: (0, 76, 57), 129: (38, 76, 66),
    130: (0, 255, 255), 131: (127, 255, 255), 132: (0, 204, 204), 133: (102, 204, 204),
    134: (0, 153, 153), 135: (76, 153, 153), 136: (0, 127, 127), 137: (63, 127, 127),
    138: (0, 76, 76), 139: (38, 76, 76), 140: (0, 191, 255), 141: (127, 223, 255),
    142: (0, 153, 204), 143: (102, 178, 204), 144: (0, 114, 153), 145: (76, 133, 153),
    146: (0, 95, 127), 147: (63, 111, 127), 148: (0, 57, 76), 149: (38, 66, 76),
    150: (0, 127, 255), 151: (127, 191, 255), 152: (0, 102, 204), 153: (102, 153, 204),
    154: (0, 76, 153), 155: (76, 114, 153), 156: (0, 63, 127), 157: (63, 95, 127),
    158: (0, 38, 76), 159: (38, 57, 76), 160: (0, 63, 255), 161: (127, 159, 255),
    162: (0, 51, 204), 163: (102, 127, 204), 164: (0, 38, 153), 165: (76, 95, 153),
    166: (0, 31, 127), 167: (63, 79, 127), 168: (0, 19, 76), 169: (38, 47, 76),
    170: (0, 0, 255), 171: (127, 127, 255), 172: (0, 0, 204), 173: (102, 102, 204),
    174: (0, 0, 153), 175: (76, 76, 153), 176: (0, 0, 127), 177: (63, 63, 127),
    178: (0, 0, 76), 179: (38, 38, 76), 180: (63, 0, 255), 181: (159, 127, 255),
    182: (51, 0, 204), 183: (127, 102, 204), 184: (38, 0, 153), 185: (95, 76, 153),
    186: (31, 0, 127), 187: (79, 63, 127), 188: (19, 0, 76), 189: (47, 38, 76),
    190: (127, 0, 255), 191: (191, 127, 255), 192: (102, 0, 204), 193: (153, 102, 204),
    194: (76, 0, 153), 195: (114, 76, 153), 196: (63, 0, 127), 197: (95, 63, 127),
    198: (38, 0, 76), 199: (57, 38, 76), 200: (191, 0, 255), 201: (223, 127, 255),
    202: (153, 0, 204), 203: (178, 102, 204), 204: (114, 0, 153), 205: (133, 76, 153),
    206: (95, 0, 127), 207: (111, 63, 127), 208: (57, 0, 76), 209: (66, 38, 76),
    210: (255, 0, 255), 211: (255, 127, 255), 212: (204, 0, 204), 213: (204, 102, 204),
    214: (153, 0, 153), 215: (153, 76, 153), 216: (127, 0, 127), 217: (127, 63, 127),
    218: (76, 0, 76), 219: (76, 38, 76), 220: (255, 0, 191), 221: (255, 127, 223),
    222: (204, 0, 153), 223: (204, 102, 178), 224: (153, 0, 114), 225: (153, 76, 133),
    226: (127, 0, 95), 227: (127, 63, 111), 228: (76, 0, 57), 229: (76, 38, 66),
    230: (255, 0, 127), 231: (255, 127, 191), 232: (204, 0, 102), 233: (204, 102, 153),
    234: (153, 0, 76), 235: (153, 76, 114), 236: (127, 0, 63), 237: (127, 63, 95),
    238: (76, 0, 38), 239: (76, 38, 57), 240: (255, 0, 63), 241: (255, 127, 159),
    242: (204, 0, 51), 243: (204, 102, 127), 244: (153, 0, 38), 245: (153, 76, 95),
    246: (127, 0, 31), 247: (127, 63, 79), 248: (76, 0, 19), 249: (76, 38, 47),
    250: (51, 51, 51), 251: (91, 91, 91), 252: (132, 132, 132), 253: (173, 173, 173),
    254: (214, 214, 214), 255: (220, 224, 228)
}

# Standard Engineering / Plant 3D Layer Palette
STANDARD_LAYER_COLORS = {
    '0': (215, 220, 225),
    'DEFPOINTS': (130, 135, 140),
    # Earthwork, Civil & Topography
    'TERRA': (168, 138, 98),
    'TERRENO': (168, 138, 98),
    'TERRAPLANAGEM': (176, 145, 102),
    'ESTUDO': (176, 145, 102),
    'VIZINHO': (185, 155, 115),
    'TOPOGRAFIA': (135, 155, 110),
    'TALUDE': (155, 125, 85),
    'CURVAS': (145, 130, 95),
    'NIVEL': (145, 130, 95),
    'CIVIL': (180, 175, 165),         # Concrete / Civil (Warm Gray)
    'CONC': (180, 175, 165),
    'CONCRETO': (180, 175, 165),
    'BASE': (180, 175, 165),
    'FOUNDATION': (180, 175, 165),
    'FUNDACAO': (180, 175, 165),
    'PISO': (175, 170, 160),
    'ALVENARIA': (200, 160, 140),
    # Equipment
    'EQUIP': (40, 145, 215),          # Equipment (Cyan/Blue)
    'EQUIPMENT': (40, 145, 215),
    'EQUIPAMENTO': (40, 145, 215),
    'TANQUE': (40, 145, 215),          # Tanks (Blue)
    'VESSEL': (40, 145, 215),
    'TANK': (40, 145, 215),
    'VASO': (40, 145, 215),
    'BOMBA': (225, 115, 35),           # Pumps (Amber/Orange)
    'PUMP': (225, 115, 35),
    'COMPRESSOR': (215, 105, 30),
    'TROCADOR': (35, 175, 165),        # Heat Exchanger (Teal)
    'COOLER': (35, 175, 165),
    'CALDEIRA': (190, 75, 60),
    # Piping & Valves
    'PIPE': (48, 175, 78),             # Piping (Green)
    'PIPING': (48, 175, 78),
    'TUB': (48, 175, 78),
    'TUBO': (48, 175, 78),
    'TUBULACAO': (48, 175, 78),
    'SPOOL': (48, 175, 78),
    'VALVE': (228, 65, 45),            # Valves (Red/Orange)
    'VALV': (228, 65, 45),
    'VALVULA': (228, 65, 45),
    'VALVULAS': (228, 65, 45),
    'FLANGE': (165, 175, 185),         # Flanges (Metallic light gray)
    'CONEXAO': (65, 160, 95),
    'FITTING': (65, 160, 95),
    'CURVA': (55, 165, 85),
    'REDUCAO': (65, 160, 95),
    'TEE': (65, 160, 95),
    'SUPP': (220, 130, 40),            # Pipe supports (Orange)
    'SUPORTE': (220, 130, 40),
    'SUPPORT': (220, 130, 40),
    'HANG': (220, 130, 40),
    # Structural Steel
    'STEEL': (100, 130, 165),          # Structural Steel (Steel Blue)
    'STR': (100, 130, 165),
    'ESTRUTURA': (100, 130, 165),
    'ESTRUTURAL': (100, 130, 165),
    'PERFIL': (100, 130, 165),
    'VIGA': (100, 130, 165),
    'BEAM': (100, 130, 165),
    'PILAR': (90, 120, 155),
    'COLUMN': (90, 120, 155),
    'CONTRAVENTAMENTO': (110, 140, 175),
    'BRACE': (110, 140, 175),
    'BRACING': (110, 140, 175),
    'CHAPA': (125, 140, 155),
    'PLATE': (125, 140, 155),
    'ESCADA': (130, 145, 160),
    'STAIR': (130, 145, 160),
    'GUARDACORPO': (210, 175, 45),     # Handrails (Safety Yellow)
    'CORRIMAO': (210, 175, 45),
    'HANDRAIL': (210, 175, 45),
    'GRADE': (140, 150, 160),
    # Electrical & Instrumentation
    'ELEC': (240, 185, 30),            # Electrical / Trays (Yellow/Gold)
    'ELETRICA': (240, 185, 30),
    'ELECTRICAL': (240, 185, 30),
    'LEITO': (240, 185, 30),
    'BANDEJA': (240, 185, 30),
    'TRAY': (240, 185, 30),
    'CABO': (215, 160, 25),
    'INSTR': (205, 70, 175),           # Instrumentation (Magenta/Purple)
    'INSTRUMENTO': (205, 70, 175),
    'INSTRUMENTATION': (205, 70, 175),
    # Utilities
    'DRENAGEM': (55, 145, 195),
    'DRAIN': (55, 145, 195),
    'AGUA': (65, 155, 225),
    'WATER': (65, 155, 225),
    'ESGOTO': (135, 105, 75),
    'AR': (130, 180, 220),
    'AIR': (130, 180, 220),
    'GAS': (235, 190, 45),
    'INCENDIO': (225, 45, 45),
    'FIRE': (225, 45, 45),
}

# Standard IFC Class to Color
IFC_CLASS_COLORS = {
    'IFCWALL': (210, 215, 220),
    'IFCWALLSTANDARDCASE': (210, 215, 220),
    'IFCSLAB': (190, 195, 200),
    'IFCBEAM': (85, 115, 145),
    'IFCCOLUMN': (75, 105, 135),
    'IFCMEMBER': (95, 125, 155),
    'IFCPLATE': (130, 140, 150),
    'IFCFLOWSEGMENT': (50, 165, 90),
    'IFCFLOWFITTING': (45, 150, 80),
    'IFCFLOWCONTROLLER': (225, 75, 55),
    'IFCVALVE': (225, 75, 55),
    'IFCPUMP': (220, 120, 35),
    'IFCTANK': (60, 140, 210),
    'IFCDOOR': (160, 120, 80),
    'IFCWINDOW': (150, 200, 230),
    'IFCRAILING': (120, 130, 140),
}


def deterministic_color(name: str) -> Tuple[int, int, int]:
    """Generate a consistent, aesthetically pleasing CAD color from a name string."""
    import hashlib
    h = int(hashlib.md5(name.encode('utf-8', errors='ignore')).hexdigest()[:6], 16)
    # Pick from the middle/vibrant range of ACI colors (10 to 240)
    aci_idx = 10 + (h % 230)
    return ACI_TABLE.get(aci_idx, (180, 190, 200))


def resolve_layer_color(layer_name: str) -> Tuple[int, int, int]:
    """Find the best color for a layer name based on engineering conventions or hash."""
    clean = layer_name.strip().upper()
    # Check exact match
    if clean in STANDARD_LAYER_COLORS:
        return STANDARD_LAYER_COLORS[clean]
    # Check keyword substring match
    for key, color in STANDARD_LAYER_COLORS.items():
        if key in clean:
            return color
    # Fallback to deterministic pleasant palette color
    return deterministic_color(clean)


def resolve_ifc_color(ifc_type: str) -> Tuple[int, int, int]:
    """Resolve color for an IFC Product type."""
    clean = ifc_type.strip().upper().replace(' ', '')
    for key, color in IFC_CLASS_COLORS.items():
        if key in clean:
            return color
    return deterministic_color(clean)


class LayerManager:
    """Tracks layers across loaded models, their colors, visibility, and overrides."""
    def __init__(self):
        self.layers: Dict[str, Dict] = {}  # {layer_name: {'color': (r,g,b), 'visible': bool, 'count': int}}

    def register(self, layer_name: str, color: Optional[Tuple[int, int, int]] = None) -> Tuple[int, int, int]:
        clean = layer_name.strip()
        if not clean:
            clean = '0'
        if clean not in self.layers:
            resolved = color if color is not None else resolve_layer_color(clean)
            self.layers[clean] = {
                'color': resolved,
                'visible': True,
                'count': 1
            }
        else:
            self.layers[clean]['count'] += 1
            if color is not None:
                self.layers[clean]['color'] = color
        return self.layers[clean]['color']

    def get_color(self, layer_name: str) -> Tuple[int, int, int]:
        clean = layer_name.strip() or '0'
        if clean in self.layers:
            return self.layers[clean]['color']
        return resolve_layer_color(clean)

    def set_color(self, layer_name: str, rgb: Tuple[int, int, int]):
        clean = layer_name.strip() or '0'
        if clean in self.layers:
            self.layers[clean]['color'] = rgb
        else:
            self.layers[clean] = {'color': rgb, 'visible': True, 'count': 0}

    def is_visible(self, layer_name: str) -> bool:
        clean = layer_name.strip() or '0'
        return self.layers.get(clean, {}).get('visible', True)

    def set_visible(self, layer_name: str, visible: bool):
        clean = layer_name.strip() or '0'
        if clean in self.layers:
            self.layers[clean]['visible'] = visible
