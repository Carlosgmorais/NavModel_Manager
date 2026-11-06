"""Native Qt Ribbon based on the approved NavModel Manager reference."""
from PySide6 import QtCore, QtGui, QtWidgets as W
from .icons import create_navis_icon
from .branding import resource

class NavisRibbonGroup(W.QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName('ribbonGroup')
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(5,3,5,2); layout.setSpacing(1)
        self.content_layout = W.QHBoxLayout(); self.content_layout.setSpacing(2)
        layout.addLayout(self.content_layout)
        label = W.QLabel(title); label.setObjectName('groupTitle')
        label.setAlignment(QtCore.Qt.AlignCenter); layout.addWidget(label)

    def add_large_button(self, text, icon, callback, tooltip='', shortcut=None, primary=False):
        btn = W.QToolButton(self)
        btn.setText(text); btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextUnderIcon)
        btn.setIcon(create_navis_icon(icon,32)); btn.setIconSize(QtCore.QSize(26,26))
        btn.setFixedSize(82 if primary else (82 if text == 'Propriedades' else 66), 68)
        if primary:
            btn.setProperty('primary',True)
            # Recolor the existing vector command icon, not the brand artwork.
            pix = btn.icon().pixmap(32,32)
            p = QtGui.QPainter(pix); p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
            p.fillRect(pix.rect(), QtCore.Qt.white); p.end(); btn.setIcon(QtGui.QIcon(pix))
        btn.clicked.connect(callback)
        btn.setToolTip(tooltip + (f' ({shortcut})' if shortcut else ''))
        if shortcut: btn.setShortcut(QtGui.QKeySequence(shortcut))
        self.content_layout.addWidget(btn)
        return btn

class NavisRibbonBar(W.QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setObjectName('navRibbon'); self.setFixedHeight(177)
        self.setStyleSheet('''
            QWidget#navRibbon {background:#062c50;}
            QWidget#quickAccess, QWidget#quickAccess QLabel {background:#062c50; color:white;}
            QWidget#quickAccess QToolButton {background:transparent; color:white; border:0; border-radius:4px; padding:3px;}
            QWidget#quickAccess QToolButton:hover {background:#1565a8;}
            QTabWidget::tab-bar {left:62px;}
            QTabWidget::pane {border:1px solid #bacbdd; background:#f3f6f9; border-radius:6px;}
            QTabBar::tab {background:#062c50; color:white; border:0; padding:5px 18px; font-size:15px;}
            QTabBar::tab:selected {background:#f3f6f9; color:#082d50; border-top-left-radius:6px; border-top-right-radius:6px; font-weight:600;}
            QTabBar::tab:hover:!selected {background:#155183;}
            QFrame#ribbonGroup {background:#f8fafc; border:0; border-right:1px solid #cbd5e1;}
            QLabel#groupTitle {background:transparent; color:#092c4e; font-size:12px; font-weight:600;}
            QToolButton {color:#0b2f50; background:transparent; border:1px solid transparent; border-radius:5px; padding:2px; font-size:12px;}
            QToolButton:hover {background:#d9edff; border:1px solid #9acaef;}
            QToolButton:checked, QToolButton:pressed {background:#a2d7f6; border-color:#81bde7;}
            QToolButton[primary="true"] {color:white; background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #0788ff,stop:1 #0049bd); font-size:12px;}
            QToolButton[primary="true"]:hover {background:#1979dd;}
            QScrollArea {border:0; background:#f3f6f9;}
        ''')
        main = W.QVBoxLayout(self); main.setContentsMargins(3,0,3,4); main.setSpacing(0)
        header = W.QWidget(); header.setObjectName('quickAccess')
        row = W.QHBoxLayout(header); row.setContentsMargins(7,0,8,0); row.setSpacing(10)
        self.brand_button = W.QToolButton(); self.brand_button.setObjectName('nvMenu')
        self.brand_button.setAccessibleName('Menu NV'); self.brand_button.setToolTip('NavModel Manager — Menu do projeto')
        self.brand_button.setIcon(QtGui.QIcon(resource('logo-white.png')))
        self.brand_button.setIconSize(QtCore.QSize(29,32)); self.brand_button.setFixedSize(64,39)
        self.brand_button.setPopupMode(W.QToolButton.InstantPopup)
        menu = W.QMenu(self.brand_button)
        for title, callback in [('Novo projeto',window.new_project),('Abrir projeto...',window.open_project),('Salvar projeto...',window.save),('Adicionar modelos...',window.add_files),('Sair',window.close)]:
            menu.addAction(title, callback)
        self.brand_button.setMenu(menu); row.addWidget(self.brand_button)
        for text, icon, callback in [('Novo','new',window.new_project),('Abrir','open',window.open_project),('Salvar','save',window.save),('Desfazer vista','reset',window.undo_camera)]:
            b = W.QToolButton(); b.setIcon(create_navis_icon(icon,24)); b.setToolTip(text)
            pix=b.icon().pixmap(24,24); p=QtGui.QPainter(pix); p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn); p.fillRect(pix.rect(),QtCore.Qt.white); p.end(); b.setIcon(QtGui.QIcon(pix))
            b.clicked.connect(callback); row.addWidget(b)
        row.addStretch()
        title = W.QLabel('Novo modelo — NavModel Manager'); self.project_title = title; title.setStyleSheet('font-size:14px; font-weight:600;')
        row.addWidget(title); row.addStretch()
        main.addWidget(header)
        self.tabs = W.QTabWidget(); main.addWidget(self.tabs)
        # Reserved corner space puts Início to the right of NV, never underneath it.
        
        home = self.page('Início')
        project = self.group(home, 'Projeto')
        self.append_button = project.add_large_button('Append','append',window.add_files,'Adicionar modelos à maquete','Ctrl+I',True)
        self.refresh_button = project.add_large_button('Refresh','refresh',window.refresh_models,'Recarregar modelos mantendo câmera e visibilidade','F5',True)
        for args in [('Novo','new',window.new_project,'Novo projeto','Ctrl+N'),('Abrir','open',window.open_project,'Abrir projeto','Ctrl+O'),('Salvar','save',window.save,'Salvar projeto','Ctrl+S'),('Salvar\ncomo','save',window.save_as,'Salvar em outro arquivo',None)]:
            project.add_large_button(*args)
        sel = self.group(home,'Seleção')
        b = sel.add_large_button('Selecionar','select',window.enable_selection,'Seleção direta por clique; arrastar continua navegando')
        b.setCheckable(True); window.selection_button = b
        sel.add_large_button('Selecionar\ntudo','tree',window.select_all_items,'Selecionar todos os componentes')
        sel.add_large_button('Selecionar\nigual','select',window.select_same_layer,'Selecionar a mesma camada')
        sel.add_large_button('Inverter\nseleção','tree',window.invert_selection,'Inverter seleção na árvore')
        vis = self.group(home,'Visibilidade')
        for t,i,c in [('Ocultar','hide',window.toggle_hide_selected),('Mostrar','unhide',window.show_all),('Isolar','isolate',window.isolate)]:
            vis.add_large_button(t,i,c)
        nav = self.group(home,'Navegação')
        nav.add_large_button('View\nCube','isolate',window.toggle_cube,'Mostrar ou ocultar cubo clicável')
        nav.add_large_button('HUD','properties',window.toggle_hud,'Coordenadas da câmera')
        nav.add_large_button('Enquadrar','reset',window.plot.reset_camera,'Enquadrar modelos','F')
        tool = self.group(home,'Ferramentas')
        for t,i,c in [('Propriedades','properties',window.show_properties),('Camadas','layers',window.show_layer_manager),('Cortes','select',window.section_tool.toggle_move),('Medir','measure',window.toggle_measure),('Opções','gpu',window.show_gpu_info)]:
            tool.add_large_button(t,i,c)
        view = self.page('Visualização')
        g = self.group(view,'Câmera')
        for t,i,c in [('Isométrica','reset',window.view_isometric_smart),('Topo','reset',window.view_top_smart),('Frente','reset',window.view_front_smart),('Direita','reset',window.view_right_smart),('Perspectiva','isolate',window.set_perspective),('Ortogonal','isolate',window.set_orthogonal),('Desfazer\nvista','reset',window.undo_camera)]: g.add_large_button(t,i,c)
        review = self.page('Revisão'); g = self.group(review,'Medição')
        g.add_large_button('Medir','measure',window.toggle_measure,'Medir entre pontos','M')
        g.add_large_button('Limpar','reset',window.clear_measures)
        window.section_tool.add_ribbon(self)
        # Preserve honest capability boundaries: unimplemented features are visibly disabled.
        animation = self.page('Animação'); g = self.group(animation,'Animação')
        b = g.add_large_button('Animação','look',lambda:None,'Não disponível nesta versão'); b.setEnabled(False)
        panels = self.page('Vista'); g = self.group(panels,'Painéis')
        g.add_large_button('Árvore','tree',window.toggle_selection_tree)
        g.add_large_button('Propriedades','properties',window.show_properties)
        g.add_large_button('Atividade','properties',window.toggle_activity)
        output = self.page('Saída'); g = self.group(output,'Exportação')
        g.add_large_button('Captura','save',window.export_view,'Salvar imagem do viewport 3D')
        self.publish_button = g.add_large_button('Publicar\nMaquete Web','save',window.publish_web,'Analisar e publicar HTML offline ou EXE portátil')
        self.publish_button.setFixedWidth(108)
        tools = self.page('Ferramentas'); g = self.group(tools,'Ferramentas')
        g.add_large_button('Camadas','layers',window.show_layer_manager)
        g.add_large_button('Diagnóstico\nGPU','gpu',window.show_gpu_info)
        settings = self.page('Configurações'); g = self.group(settings,'Unidade para arquivos sem unidade')
        window.units = W.QComboBox()
        for label,scale in [('mm',.001),('m',1.),('cm',.01),('pol',.0254)]: window.units.addItem(label,scale)
        g.content_layout.addWidget(window.units)

    def page(self,title):
        scroll = W.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        page = W.QWidget(); row = W.QHBoxLayout(page); row.setContentsMargins(5,2,5,2); row.setSpacing(2)
        row.setSizeConstraint(W.QLayout.SetMinimumSize); row.addStretch()
        scroll.setWidget(page); self.tabs.addTab(scroll,title)
        return row

    def group(self,row,title):
        g = NavisRibbonGroup(title); row.insertWidget(row.count()-1,g); return g
