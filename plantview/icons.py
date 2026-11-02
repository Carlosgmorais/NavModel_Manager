"""Resolution-independent navy command glyphs; branding uses approved raster assets."""
from PySide6 import QtCore, QtGui

def create_navis_icon(name, size=32):
    if name == 'app_logo':
        from .branding import app_icon
        return app_icon()
    pix = QtGui.QPixmap(size,size); pix.fill(QtCore.Qt.transparent)
    p=QtGui.QPainter(pix); p.setRenderHint(QtGui.QPainter.Antialiasing); p.scale(size/32,size/32)
    p.setPen(QtGui.QPen(QtGui.QColor('#0b2f50'),1.8,QtCore.Qt.SolidLine,QtCore.Qt.RoundCap,QtCore.Qt.RoundJoin))
    p.setBrush(QtCore.Qt.NoBrush)
    def line(a,b): p.drawLine(QtCore.QPointF(*a),QtCore.QPointF(*b))
    def poly(points): p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(*v) for v in points]))
    def cube():
        poly([(16,3),(28,9),(28,23),(16,29),(4,23),(4,9)])
        line((4,9),(16,15));line((16,15),(28,9));line((16,15),(16,29))
    if name in ('new','append'):
        poly([(7,3),(20,3),(26,9),(26,28),(7,28)])
        line((20,3),(20,10));line((20,10),(26,10))
        if name=='append':
            p.fillRect(17,18,15,14,QtGui.QColor('white'))
            line((24,19),(24,29));line((19,24),(29,24))
    elif name=='open':
        p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(*v) for v in [(3,25),(3,8),(13,8),(16,12),(28,12)]]))
        poly([(3,26),(8,14),(30,14),(25,26)])
    elif name=='save':
        p.drawRoundedRect(4,4,24,24,1,1);p.drawRect(9,4,14,9);p.drawRect(9,19,14,9)
    elif name=='refresh':
        p.drawArc(5,5,22,22,25*16,145*16);p.drawArc(5,5,22,22,205*16,145*16)
        poly([(24,3),(28,10),(20,10)]);poly([(8,29),(4,22),(12,22)])
    elif name=='select':
        poly([(7,3),(7,26),(13,20),(19,29),(23,26),(17,18),(27,17)])
    elif name in ('tree','reset'):
        p.setPen(QtGui.QPen(QtGui.QColor('#0b2f50'),1.8,QtCore.Qt.DashLine))
        p.drawRect(5,5,22,22)
        if name=='tree':
            p.setPen(QtGui.QPen(QtGui.QColor('#0b2f50'),1.8));line((12,16),(16,20));line((16,20),(23,11))
    elif name in ('hide','unhide','isolate'):
        cube()
        if name=='hide': line((3,29),(29,3))
        elif name=='unhide':
            p.setBrush(QtGui.QColor('#86cff3'));poly([(16,15),(28,9),(28,23),(16,29)])
    elif name=='layers':
        for y in (4,11,18): poly([(16,y),(29,y+6),(16,y+12),(3,y+6)])
    elif name=='measure':
        poly([(4,10),(10,4),(28,22),(22,28)])
        for x,y in [(10,10),(15,15),(20,20)]:line((x,y),(x+3,y-3))
    elif name=='properties':
        p.drawEllipse(3,3,26,26);line((16,14),(16,23));p.drawEllipse(15,8,2,2)
    elif name=='gpu':
        p.drawEllipse(8,8,16,16);p.drawEllipse(12,12,8,8)
        import math
        for i in range(8):
            a=i*math.pi/4
            line((16+9*math.cos(a),16+9*math.sin(a)),(16+14*math.cos(a),16+14*math.sin(a)))
    elif name in ('orbit','look'):
        p.drawEllipse(3,9,26,14);p.drawEllipse(11,11,10,10)
    else: cube()
    p.end();return QtGui.QIcon(pix)
