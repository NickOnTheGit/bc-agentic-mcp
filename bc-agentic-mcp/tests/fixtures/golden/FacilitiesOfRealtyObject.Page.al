namespace Zig.Property;

page 11030034 FacilitiesOfRealtyObjectFDN
{
    ApplicationArea = All;
    Caption = 'Facilities of Lettable Object', Comment = 'DevOps221157';
    DataCaptionExpression = RealtyObject.GenerateShortCaption();
    DelayedInsert = true;
    PageType = List;
    SourceTable = RealtyObjectFacilityFDN;
    SourceTableView = sorting(RealtyObjectNo, FacilityCode);

    layout
    {
        area(Content)
        {
            repeater(General)
            {
                field(FacilityCode; Rec.FacilityCode) { }
                field(FacilityGroup; Rec.FacilityGroup) { }
                field(FacilityType; Rec.FacilityType) { }
                field(FacilityName; Rec.FacilityName) { }
                field(FacilityDescription; Rec.FacilityDescription) { }
                field(FacilityMeasuringValue; Rec.GetMeasuringValueText())
                {
                    Caption = 'Measuring Value', Comment = 'DevOps221157';
                    ToolTip = 'Specifies the measuring value that is applicable for this facility and this lettable object.', Comment = 'DevOps221157';

                    trigger OnDrillDown()
                    begin
                        Rec.SetMeasuringValue();
                    end;
                }
                field(FacilityMeasuringUnit; Rec.FacilityMeasuringUnit) { }
                field(NoOfAddresses; Rec.NoOfAddresses)
                {
                    Editable = Rec.FacilityGroup <> Rec.FacilityGroup::Special;
                }
                field(NoOfSharedAccommodations; Rec.NoOfSharedAccommodations)
                {
                    Editable = IsSharedAccommodation;
                    Visible = IsSharedAccommodation;
                }
                field(DataSource; Rec.DataSource) { }
            }
        }
    }

    trigger OnOpenPage()
    begin
        Rec.FilterGroup(2);
        RealtyObject.Get(Rec.GetFilter(RealtyObjectNo));
        Rec.FilterGroup(0);
        Rec.SetAutoCalcFields(FacilityGroup);

        IsSharedAccommodation := RealtyObject.Woonruimte = RealtyObject.Woonruimte::SharedAccommodation;
    end;

    trigger OnNewRecord(BelowxRec: Boolean)
    begin
        InitNoOfSharedAccommodations();
    end;

    var
        RealtyObject: Record OGE;
        IsSharedAccommodation: Boolean;

    local procedure InitNoOfSharedAccommodations()
    begin
        if RealtyObject.Woonruimte = RealtyObject.Woonruimte::SharedAccommodation then
            Rec.NoOfSharedAccommodations := 1;
    end;
}