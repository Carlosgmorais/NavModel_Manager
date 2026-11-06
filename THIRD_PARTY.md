# Componentes de terceiros

O PlantView Studio é um protótipo independente. Autodesk, AutoCAD, Navisworks, Revit e DWG são nomes de seus respectivos titulares; nenhuma afiliação é sugerida.

## Leitor DWG livre incorporado

- **acadrust**, Hakan AK e colaboradores, MPL-2.0. Fonte incorporada em `dwg-reader/vendor/acadrust`, revisão `ef8ba01`. Origem: https://github.com/HakanSeven12/acadrust
- **opencadkernel**, autores e colaboradores do projeto, MPL-2.0. Fonte incorporada em `dwg-reader/vendor/opencadkernel`, revisão `64a5d0e`. Origem: https://github.com/HakanSeven12/opencadkernel
- A biblioteca acadrust usada pelo kernel é direcionada à mesma cópia local por `[patch]` no Cargo.toml. O leitor PlantView é código novo que usa as APIs dessas bibliotecas. As fontes dos fornecedores não foram intencionalmente alteradas.
- As licenças originais estão nas pastas correspondentes. `Cargo.lock` lista os demais pacotes Rust usados na compilação.

O Open CAD Studio foi consultado para identificar essas bibliotecas e entender suas APIs: https://github.com/HakanSeven12/OpenCADStudio. O executável e a interface do Open CAD Studio **não** fazem parte da entrega final.

## Interface e geometria

- Three.js r160 (MIT), incorporado ao visualizador Web em `resources/web/three.min.js`. O aviso de licença está em `resources/web/THREE-LICENSE.txt`.
- O servidor do visualizador portátil inclui o runtime .NET 8. A biblioteca padrão é distribuída conforme os avisos do runtime em https://github.com/dotnet/runtime/blob/v8.0.0/THIRD-PARTY-NOTICES.TXT.

- Python 3.12: PSF, https://www.python.org/
- PySide6/Qt: LGPL-3.0/GPL/comercial conforme componente, https://doc.qt.io/qtforpython-6/
- VTK: BSD, https://vtk.org/
- PyVista e pyvistaqt: MIT, https://docs.pyvista.org/ e https://github.com/pyvista/pyvistaqt
- Open CASCADE/OCP: licenças dos projetos (OCCT LGPL-2.1 com exceção), https://dev.opencascade.org/ e https://github.com/CadQuery/OCP
- IfcOpenShell: LGPL, https://ifcopenshell.org/
- ezdxf: MIT, https://ezdxf.readthedocs.io/
- NumPy, Matplotlib e demais pacotes: licenças respectivas, preservadas nos metadados e na pasta de licenças da distribuição.
- PyInstaller: GPL com exceção para executáveis gerados, https://pyinstaller.org/

As bibliotecas dinâmicas são distribuídas separadamente na pasta `_internal`; o código-fonte e as instruções de rebuild acompanham a entrega. Não se usa SDK ODA/RealDWG pago. Nenhuma licença Autodesk é exigida para executar este protótipo.
